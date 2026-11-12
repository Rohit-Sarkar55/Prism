"""A narrated walk through the whole gateway, in the order of the brief's demo checklist.

    python -m tools.dev_stack --fresh-db     # terminal 1: start the stack on a clean database, leave it running
    python -m tools.demo                     # terminal 2: press Enter between steps while you talk
    python -m tools.demo --no-pause          # run straight through

Open http://127.0.0.1:8080/console beside it (admin token: demo-admin-token) and watch it fill in. The demo
needs a clean database (an unspent budget key and an unused rate-limit window), so restart the stack with
--fresh-db before each run; it checks this first. Plain ASCII output on purpose: it must look right in any console.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass

import httpx

from app.config import PROJECT_ROOT, get_settings
from tools import dev_stack

SEED = json.loads((PROJECT_ROOT / "data" / "seed_keys.json").read_text(encoding="utf-8"))["tenants"]
KEY = {t["team"]: t["virtual_key"] for t in SEED}

ROSTER = (
    "Here is our on-call roster for the next two weeks: Monday - Priya, Tuesday - Chen, Wednesday - Amara, "
    "Thursday - Diego, Friday - Fatima, Saturday - Lukas, Sunday - Mei, next Monday - Tom, next Tuesday - Sara, "
    "next Wednesday - Ravi. Who is on call this Thursday?"
)


# ---- small pure helpers (unit-tested) ---------------------------------------------------------------------


def fmt_cost(header: str | None) -> str:
    """'0.000011700' -> '$0.0000117', '0' or missing -> '$0'."""
    if not header or float(header) == 0:
        return "$0"
    return "$" + header.rstrip("0").rstrip(".")


def problems_with_state(keys: list[dict]) -> list[str]:
    """The demo tells a clean story only on an empty database: every key unused this month."""
    used = [f"{k['team']} already has {k['requests']} request(s)" for k in keys if k["requests"] > 0]
    return used


def describe(resp: httpx.Response) -> str:
    h = resp.headers
    served = h.get("x-prism-provider", "none")
    return f"HTTP {resp.status_code}  served by {served:<18} cache {h.get('x-prism-cache', '-'):<4} cost {fmt_cost(h.get('x-prism-cost-usd'))}"


@dataclass
class Demo:
    client: httpx.Client
    base: str
    admin: dict
    pause: bool

    def ask(self, team: str, text: str, model: str = "fast") -> httpx.Response:
        body = {"model": model, "messages": [{"role": "user", "content": text}]}
        return self.client.post(
            f"{self.base}/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {KEY[team]}"}
        )

    def admin_get(self, path: str, **params):
        resp = self.client.get(f"{self.base}{path}", params=params, headers=self.admin)
        resp.raise_for_status()
        return resp.json()

    def last_log(self, team: str) -> dict:
        return self.admin_get("/admin/logs", key=KEY[team], limit=1)["logs"][0]

    def mock(self, name: str, **config) -> None:
        port = dev_stack.ALPHA_PORT if name == "alpha" else dev_stack.BETA_PORT
        self.client.post(f"http://127.0.0.1:{port}/admin/config", json=config).raise_for_status()

    def step(self, number: int, total: int, title: str, intro: str) -> None:
        print(f"\n{'=' * 78}\nSTEP {number}/{total}: {title}\n{'=' * 78}\n{intro}\n")

    def line(self, label: str, resp: httpx.Response) -> None:
        print(f"  {label:<52} {describe(resp)}")

    def wait(self) -> None:
        if self.pause:
            input("\n  [press Enter for the next step] ")


# ---- the steps ------------------------------------------------------------------------------------------


def step_streaming(d: Demo) -> None:
    d.step(
        1,
        6,
        "Streaming, token by token",
        "Tokens are forwarded the moment the provider produces them: no buffering, ends with [DONE].",
    )
    body = {
        "model": "fast",
        "stream": True,
        "messages": [{"role": "user", "content": "Explain how server-sent events work in some detail please"}],
    }
    started, first, chunks = time.monotonic(), None, 0
    print("  ", end="", flush=True)
    with d.client.stream(
        "POST", f"{d.base}/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {KEY['search']}"}
    ) as resp:
        provider = resp.headers.get("x-prism-provider")
        for raw in resp.iter_lines():
            if not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            text = chunk["choices"][0]["delta"].get("content", "") if chunk.get("choices") else ""
            if text and first is None:
                first = time.monotonic() - started
            chunks += 1
            print(text, end="", flush=True)
    total = time.monotonic() - started
    row = d.last_log("search")
    print(
        f"\n\n  served by {provider}; first token after {first * 1000:.0f} ms, last after {total * 1000:.0f} ms, {chunks} chunks, stream ended with [DONE]"
    )
    print(
        f"  recorded in the log: status {row['status']}, {row['prompt_tokens']} tokens in / {row['completion_tokens']} out, cost {fmt_cost(row['cost_usd'])}"
    )


def step_routing(d: Demo) -> None:
    d.step(
        2,
        6,
        "Smart routing: model 'auto'",
        "The gateway judges how hard each prompt is. Easy goes to the cheap model, hard to the strong one. Length is not difficulty.",
    )
    prompts = [
        ("an easy question", "What is the capital of France?"),
        ("a hard one", "Prove that the square root of 2 is irrational."),
        ("a LONG input with a trivial question", ROSTER),
    ]
    for label, text in prompts:
        resp = d.ask("research", text, model="auto")
        row = d.last_log("research")
        print(f"  {label:<40} {describe(resp)}\n  {'':<40} why: {row['route_reason']}\n")


def step_cache(d: Demo) -> None:
    d.step(
        3,
        6,
        "Semantic cache",
        "A repeat costs nothing. A paraphrase is recognised by meaning. A look-alike with a different intent is not. And one team can never receive another team's cached answer.",
    )
    original = "How do I reset my password on the dashboard?"
    d.line("search key: first ask", d.ask("search", original))
    d.line("search key: exact repeat", d.ask("search", original))
    d.line(
        "search key: PARAPHRASE (same meaning, new words)",
        d.ask("search", "What are the steps to reset my dashboard password?"),
    )
    d.line(
        "search key: LOOK-ALIKE (two-factor, not password)",
        d.ask("search", "How do I reset my two-factor authentication on the dashboard?"),
    )
    d.line("free-tier key: the SAME prompt (another team)", d.ask("free-tier", original))
    print("\n  The other team got a real miss and paid for it: the cache is scoped per key, never shared.")


def step_budget(d: Demo) -> None:
    d.step(4, 6, "Budget rejection", "This key's whole monthly budget is $0.00001. One request spends more than that.")
    text = "Explain what a message queue is and when a team should use one"
    d.line("budget-demo key: first request", d.ask("budget-demo", text))
    second = d.ask("budget-demo", text)
    d.line("budget-demo key: second request", second)
    print(f"\n  rejected with HTTP 402: {second.json()['error']['message']}")


def step_rate_limit(d: Demo) -> None:
    used = next(k["requests"] for k in d.admin_get("/admin/keys")["keys"] if k["team"] == "free-tier")
    intro = f"The free-tier key is limited to 10 requests per minute (it has already used {used} in step 3)."
    d.step(5, 6, "Rate limiting", intro)
    statuses = []
    for i in range(1, 13):
        resp = d.ask("free-tier", f"Demo question number {i}: what is a mutex?")
        statuses.append(resp.status_code)
        retry = f"   Retry-After {resp.headers['retry-after']} s" if resp.status_code == 429 else ""
        print(f"  request {i:>2}: HTTP {resp.status_code}{retry}")
    accepted, limited = statuses.count(200), statuses.count(429)
    print(
        f"\n  {accepted} accepted, {limited} rate limited (type rate_limit_exceeded): {used + accepted} requests got through"
    )
    print("  in the minute, which is the key's limit. scripts/load_test.py fires a concurrent burst at the same limit.")


def step_failover(d: Demo) -> None:
    d.step(
        6,
        6,
        "Live provider failover",
        "alpha is the primary for 'fast', beta is the fallback. We break alpha while the gateway is running.",
    )
    q = "What is a message queue?"
    try:
        d.line("healthy", d.ask("research", q))
        d.mock("alpha", mode="down")
        started = time.monotonic()
        resp = d.ask("research", q)
        d.line("alpha DOWN", resp)
        print(
            f"  {'':<52} fallback header: {resp.headers.get('x-prism-fallback')}   answered in {time.monotonic() - started:.2f} s (retried with backoff, then failed over)"
        )
        d.mock("alpha", mode="ok", latency_ms=3000)
        started = time.monotonic()
        resp = d.ask("research", q)
        d.line("alpha SLOW (3 s, the timeout is 2 s)", resp)
        print(
            f"  {'':<52} fallback header: {resp.headers.get('x-prism-fallback')}   answered in {time.monotonic() - started:.2f} s (timeout, then failed over: never hangs)"
        )
    finally:
        d.mock("alpha", mode="ok", latency_ms=0, fail_rate=0)
    d.line("alpha RESTORED", d.ask("research", q))
    health = d.admin_get("/admin/providers")["providers"]
    print("\n  provider health: " + ", ".join(f"{name} {info['state']}" for name, info in health.items()))


def wrap_up(d: Demo) -> None:
    keys = d.admin_get("/admin/keys")["keys"]
    cache = d.admin_get("/admin/cache/stats")["overall"]
    print(f"\n{'=' * 78}\nDONE. What the gateway recorded during this demo\n{'=' * 78}")
    print(f"  {'team':<13}{'requests':>9}{'spend':>14}{'cache hits':>12}")
    for k in keys:
        spend = fmt_cost(f"{k['spent_usd']:.9f}")
        print(f"  {k['team']:<13}{k['requests']:>9}{spend:>14}{k['cache_hits']:>12}")
    saved = fmt_cost(f"{cache['saved_usd']:.9f}")
    print(f"\n  cache: {cache['hits']} hits, {cache['misses']} misses, saved {saved}")
    print("  Every request above, including the rejected ones, is in the request log and on the console.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-pause", action="store_true", help="do not wait for Enter between steps")
    parser.add_argument("--force", action="store_true", help="run even if the database is not clean")
    parser.add_argument("--gateway-port", type=int, default=8080)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.gateway_port}"
    demo = Demo(
        httpx.Client(timeout=30.0, trust_env=False),
        base,
        {"Authorization": f"Bearer {get_settings().admin_token}"},
        pause=not args.no_pause,
    )
    try:
        demo.client.get(f"{base}/health").raise_for_status()
        issues = problems_with_state(demo.admin_get("/admin/keys")["keys"])
    except (httpx.HTTPError, OSError) as err:
        print(
            f"Cannot reach the gateway at {base} ({type(err).__name__}). Start it first:  python -m tools.dev_stack --fresh-db"
        )
        return 1
    if issues and not args.force:
        print("The database is not clean, so this demo would not tell its story: " + "; ".join(issues) + ".")
        print("Stop the stack (Ctrl+C), restart it with  python -m tools.dev_stack --fresh-db  and run the demo again.")
        return 1

    print("Prism demo. Console: " + base + "/console   (admin token: demo-admin-token)")
    demo.wait()
    try:
        for step in (step_streaming, step_routing, step_cache, step_budget, step_rate_limit, step_failover):
            step(demo)
            demo.wait()
        wrap_up(demo)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 130
    finally:
        demo.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())