import httpx

from tools import demo


def test_costs_are_shown_without_trailing_zeros_and_zero_is_plain():
    assert demo.fmt_cost("0.000011700") == "$0.0000117"
    assert demo.fmt_cost("0.000174000") == "$0.000174"
    assert demo.fmt_cost("0") == "$0" and demo.fmt_cost("0.000000000") == "$0" and demo.fmt_cost(None) == "$0"


def test_a_clean_database_has_no_problems_and_a_used_one_names_the_keys():
    clean = [{"team": "search", "requests": 0}, {"team": "free-tier", "requests": 0}]
    assert demo.problems_with_state(clean) == []
    dirty = [
        {"team": "search", "requests": 8},
        {"team": "free-tier", "requests": 0},
        {"team": "budget-demo", "requests": 1},
    ]
    assert demo.problems_with_state(dirty) == [
        "search already has 8 request(s)",
        "budget-demo already has 1 request(s)",
    ]


def test_a_response_is_described_in_one_line_with_the_headers_that_matter():
    resp = httpx.Response(200, headers={"x-prism-provider": "cache", "x-prism-cache": "hit", "x-prism-cost-usd": "0"})
    line = demo.describe(resp)
    assert "HTTP 200" in line and "cache hit" in line and line.endswith("$0")
    paid = httpx.Response(
        200,
        headers={"x-prism-provider": "alpha/alpha-small", "x-prism-cache": "miss", "x-prism-cost-usd": "0.000014550"},
    )
    assert demo.describe(paid) == "HTTP 200  alpha/alpha-small  cache miss $0.00001455"
    rejected = httpx.Response(
        402, headers={"x-prism-provider": "none", "x-prism-cache": "miss", "x-prism-cost-usd": "0"}
    )
    assert "HTTP 402" in demo.describe(rejected) and "none" in demo.describe(rejected)


def test_the_demo_output_is_plain_ascii_so_it_looks_right_in_any_console():
    source = (demo.PROJECT_ROOT / "tools" / "demo.py").read_text(encoding="utf-8")
    assert source.isascii()


def test_the_demo_uses_the_seeded_keys_by_team_name_not_copied_strings():
    assert set(demo.KEY) == {"search", "research", "free-tier", "budget-demo"}
    source = (demo.PROJECT_ROOT / "tools" / "demo.py").read_text(encoding="utf-8")
    assert "prism-sk-" not in source


# ---- the printed layout, run against a fake gateway (no servers, no network) -------------------------------

import json
import re

LONG_REPLY = " ".join(f"word{i}" for i in range(60))  # long enough to force the streamed paragraph to wrap


def fake_gateway() -> httpx.MockTransport:
    seen: set[tuple[str, str]] = set()
    counts = {"free-tier": 0, "budget-demo": 0}
    alpha = {"mode": "ok", "latency_ms": 0}
    teams = {demo.KEY[t]: t for t in demo.KEY}

    def reply(status, provider, cache, cost, fallback="false", extra=None):
        headers = {
            "x-prism-provider": provider,
            "x-prism-cache": cache,
            "x-prism-cost-usd": cost,
            "x-prism-fallback": fallback,
        }
        body = (
            {"error": {"message": "Monthly budget of $0.00001 exhausted for this key"}}
            if status == 402
            else {"ok": True}
        )
        return httpx.Response(status, headers={**headers, **(extra or {})}, json=body)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/admin/config":
            alpha.update(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.method == "POST":
            team = teams[request.headers["authorization"].split()[-1]]
            body = json.loads(request.content)
            text = body["messages"][0]["content"]
            if body.get("stream"):
                chunks = [{"choices": [{"delta": {"content": f"word{i} "}}]} for i in range(60)]
                sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                return httpx.Response(200, headers={"x-prism-provider": "alpha/alpha-small"}, content=sse.encode())
            if team in counts:
                counts[team] += 1
                if team == "budget-demo" and counts[team] > 1:
                    return reply(402, "none", "miss", "0")
                if team == "free-tier" and counts[team] > 10:
                    return reply(429, "none", "miss", "0", extra={"retry-after": "57"})
            if (team, text) in seen:
                return reply(200, "cache", "hit", "0")
            seen.add((team, text))
            if alpha["mode"] == "down" or alpha["latency_ms"]:
                return reply(200, "beta/beta-small", "miss", "0.000009300", fallback="true")
            large = body["model"] == "auto" and text.startswith("Prove")
            return reply(200, "alpha/alpha-large" if large else "alpha/alpha-small", "miss", "0.000014550")
        if path == "/admin/logs":
            row = {
                "status": "ok",
                "prompt_tokens": 9,
                "completion_tokens": 26,
                "cost_usd": "0.000016950",
                "route_reason": "simple (score -1.5): lookup question -1.5",
            }
            return httpx.Response(200, json={"logs": [row]})
        if path == "/admin/keys":
            keys = [{"team": t, "requests": counts.get(t, 0), "spent_usd": 0.0001, "cache_hits": 0} for t in demo.KEY]
            return httpx.Response(200, json={"keys": keys})
        if path == "/admin/providers":
            return httpx.Response(
                200, json={"providers": {"alpha": {"state": "healthy"}, "beta": {"state": "healthy"}}}
            )
        return httpx.Response(200, json={"overall": {"hits": 2, "misses": 13, "saved_usd": 0.0000291}})

    return httpx.MockTransport(handler)


def run_all_steps(capsys) -> list[str]:
    client = httpx.Client(transport=fake_gateway())
    d = demo.Demo(client, "http://127.0.0.1:8080", {}, pause=False)
    for step in (
        demo.step_streaming,
        demo.step_routing,
        demo.step_cache,
        demo.step_budget,
        demo.step_rate_limit,
        demo.step_failover,
    ):
        step(d)
    demo.wrap_up(d)
    return capsys.readouterr().out.splitlines()


def test_every_line_the_demo_prints_fits_a_normal_terminal(capsys):
    lines = run_all_steps(capsys)
    too_long = [(len(line), line[:60]) for line in lines if len(line) > 100]
    assert not too_long, f"lines that would wrap on screen: {too_long}"


def test_result_rows_keep_their_columns_separated(capsys):
    lines = run_all_steps(capsys)
    # result rows come from describe(); the rate-limit tally lines and the 402 explanation are different things
    rows = [ln for ln in lines if "HTTP " in ln and not re.match(r"\s*(request\s+\d+:|rejected with)", ln)]
    assert len(rows) == 14  # 3 routing, 5 cache, 2 budget, 4 failover
    for row in rows:
        assert re.search(r"HTTP \d{3}  \S+\s+cache (hit|miss)\s+\$\S+", row), row  # no words run together


def test_the_streamed_paragraph_wraps_and_all_six_steps_appear(capsys):
    output = "\n".join(run_all_steps(capsys))
    assert all(f"STEP {n}/6" in output for n in range(1, 7)) and "DONE." in output
    streamed = [line for line in output.splitlines() if line.startswith("  word")]
    assert len(streamed) >= 2 and all(len(line) <= 96 for line in streamed)  # wrapped, not one 400-character line


def test_the_rate_limit_step_reports_exactly_the_limit(capsys):
    output = "\n".join(run_all_steps(capsys))
    assert "9 accepted, 3 rate limited" in output and "10 requests got through in the minute" in output
    assert "Retry-After 57 s" in output