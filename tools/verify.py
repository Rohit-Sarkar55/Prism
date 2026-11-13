"""One-command verification of the whole gateway. It starts a fresh private stack (empty database, both mock
providers, the gateway), runs every check the brief asks to be verified, and writes VERIFICATION.md.


    python -m tools.verify                    # everything, about two minutes
    python -m tools.verify --skip-tests       # skip the pytest run
    python -m tools.verify --output report.md # write the report somewhere else

Every number in the report comes from the run that produced it. It needs the stack's ports (8080, 9001, 9002)
to be free, so stop a running `python -m tools.dev_stack` first. Your own prism.db is never touched: the run
uses a temporary database.
"""

import argparse
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import PROJECT_ROOT, get_settings
from tools import cache_eval, dev_stack, routing_eval

SEED = json.loads((PROJECT_ROOT / "data" / "seed_keys.json").read_text(encoding="utf-8"))["tenants"]
KEY = {t["team"]: t["virtual_key"] for t in SEED}
RPM = {t["team"]: t["rate_limit"]["requests_per_minute"] for t in SEED}
PROMPT_X = "How do I rotate my API token?"


@dataclass
class Check:
    name: str
    passed: bool | None  # None = informational, no pass or fail
    summary: str
    evidence: str = ""


# ---- pure parsing and judging (unit-tested against real output) -----------------------------------------


def parse_smoke(output: str) -> dict:
    m = re.search(r"(\d+) passed, (\d+) warnings?, (\d+) failed", output)
    return {"passed": int(m[1]), "warnings": int(m[2]), "failed": int(m[3])} if m else {}


def parse_load(output: str) -> dict:
    def number(pattern: str, cast=int):
        m = re.search(pattern, output)
        return cast(m[1]) if m else None

    return {
        "accepted": number(r"accepted \(200\):\s+(\d+)"),
        "rate_limited": number(r"rate limited \(429\):\s+(\d+)"),
        "other": number(r"other outcomes:\s+(\d+)") or 0,
        "client_avg_ms": number(r"latency avg/p95 ms:\s+(\d+) /"),
        "client_p95_ms": number(r"latency avg/p95 ms:\s+\d+ / (\d+)"),
        "requests": number(r"accepted requests:\s+(\d+)"),
        "prompt_tokens": number(r"prompt tokens:\s+(\d+)"),
        "completion_tokens": number(r"completion tokens:\s+(\d+)"),
        "cost_usd": number(r"sum of cost headers:\s+([\d.]+) USD", float),
        "over_admission_ok": "Over-admission check OK" in output,
    }


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1))]


def describe(values: list[float]) -> dict:
    return {
        "n": len(values),
        "min": min(values),
        "median": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def judge_load(load: dict, limit: int, total: int) -> Check:
    accepted, limited, other = load["accepted"], load["rate_limited"], load["other"]
    ok = bool(load["over_admission_ok"]) and accepted is not None and 0 < accepted <= limit and other == 0
    ok = ok and accepted + limited + other == total
    return Check(
        "Load test: no over-admission", ok,
        f"{accepted} accepted of a limit of {limit}, {limited} rate limited, {other} other, out of {total}",
    )  # fmt: skip


def judge_accounting(load: dict, usage: dict) -> Check:
    """The client summed what it received; the gateway's usage API must agree exactly (cost to the script's rounding)."""
    pairs = [
        ("requests", load["requests"], usage["requests"]),
        ("prompt tokens", load["prompt_tokens"], usage["prompt_tokens"]),
        ("completion tokens", load["completion_tokens"], usage["completion_tokens"]),
    ]
    cost_ok = abs(usage["cost_usd"] - load["cost_usd"]) <= 5.0e-7  # the script prints its sum to 6 decimals
    ok = all(a == b for _, a, b in pairs) and cost_ok
    lines = [
        f"{name:18} client {a:>8}   usage API {b:>8}   {'match' if a == b else 'MISMATCH'}" for name, a, b in pairs
    ]
    lines.append(
        f"{'cost (USD)':18} client {load['cost_usd']:>8.6f}   usage API {usage['cost_usd']:>8.6f}   {'match' if cost_ok else 'MISMATCH'} (exact: {usage['cost_nano']} nano-USD)"
    )
    return Check("Accounting: usage API equals the client's totals", ok, "requests, tokens and cost reconcile" if ok else "totals differ", "\n".join(lines))  # fmt: skip


def render_report(meta: dict, checks: list[Check]) -> str:
    graded = [c for c in checks if c.passed is not None]
    passed = sum(1 for c in graded if c.passed)
    mark = {True: "PASS", False: "FAIL", None: "info"}
    out = [
        "# Prism verification report",
        "",
        f"Generated {meta['when']} UTC by `python -m tools.verify` on {meta['os']}, Python {meta['python']}.",
        "Every number below was produced by that run, against a fresh private stack: an empty temporary database,",
        "the two mock providers and the gateway. Nothing is copied from an earlier run.",
        "",
        "## Summary",
        "",
        "| # | Check | Result | Detail |",
        "|---|---|---|---|",
    ]
    out += [f"| {i} | {c.name} | **{mark[c.passed]}** | {c.summary} |" for i, c in enumerate(checks, 1)]
    out += ["", f"**{passed} of {len(graded)} graded checks passed.**", "", "## Details"]
    for i, c in enumerate(checks, 1):
        out += ["", f"### {i}. {c.name}  ({mark[c.passed]})", "", c.summary]
        if c.evidence:
            out += ["", "```text", c.evidence.rstrip(), "```"]
    out += [
        "",
        "## Reading the latency numbers",
        "",
        "`latency_ms` in the request log is the time inside the gateway and `upstream_ms` is the part spent",
        "waiting on the provider; their difference is what the gateway itself adds. The provided load-test script",
        "prints a larger figure because it also counts connection setup and one-time client start-up on each of",
        "its threads, so judge the gateway by the gateway's own numbers.",
        "",
        "## What this does not show",
        "",
        "- The providers are the mock providers. Real provider latency, rate limits and failure modes will differ.",
        "- One machine, one gateway process. The rate limiter and provider health are in memory, so behaviour",
        "  with several instances is not covered.",
        "- The routing and cache labels are the author's judgment, and the rules were tuned after seeing the first",
        "  results (the README says which numbers are independent and which are not).",
        "- The eval sets are small (20 and 40 prompts, 52 cache pairs): accuracy figures carry real uncertainty.",
        "",
    ]
    return "\n".join(out)


# ---- the checks that talk to the running stack -----------------------------------------------------------


class Stack:
    def __init__(self, port: int, db_path: Path):
        self.port, self.db_path = port, db_path
        self.base = f"http://127.0.0.1:{port}"
        self.client = httpx.Client(timeout=30.0, trust_env=False)  # no system proxy: this is local traffic
        self.admin = {"Authorization": f"Bearer {get_settings().admin_token}"}
        self.procs: dict = {}

    def chat(self, team: str, text: str, model: str = "fast", stream: bool = False) -> httpx.Response:
        body = {"model": model, "messages": [{"role": "user", "content": text}], **({"stream": True} if stream else {})}
        return self.client.post(
            f"{self.base}/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {KEY[team]}"}
        )

    def admin_get(self, path: str, **params):
        resp = self.client.get(f"{self.base}{path}", params=params, headers=self.admin)
        resp.raise_for_status()
        return resp.json()

    def mock(self, name: str, **config) -> None:
        port = dev_stack.ALPHA_PORT if name == "alpha" else dev_stack.BETA_PORT
        self.client.post(f"http://127.0.0.1:{port}/admin/config", json=config).raise_for_status()

    def run_script(self, args: list[str], timeout: int) -> tuple[int, str]:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        done = subprocess.run(
            [sys.executable, *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
        return done.returncode, done.stdout + done.stderr


def check_unit_tests() -> Check:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    last = lines[-1].strip("= ") if lines else "no output"
    return Check("Automated tests", done.returncode == 0, last, "\n".join(lines[-12:]))


def check_smoke(stack: Stack) -> Check:
    code, out = stack.run_script(
        ["scripts/smoke_test.py", "--url", stack.base, "--key", KEY["search"], "--model", "fast"], 180
    )
    result = parse_smoke(out)
    ok = code == 0 and bool(result) and result["failed"] == 0
    summary = (
        f"{result['passed']} passed, {result['warnings']} warnings, {result['failed']} failed"
        if result
        else "could not read the result"
    )
    return Check("Provided smoke test", ok, summary, out)


def check_budget(stack: Stack) -> Check:
    text = "Explain what a message queue is and when a team should use one"
    first, second = stack.chat("budget-demo", text), stack.chat("budget-demo", text)
    error = second.json().get("error", {}) if second.status_code != 200 else {}
    ok = first.status_code == 200 and second.status_code == 402 and error.get("type") == "budget_exceeded"
    evidence = f"request 1: HTTP {first.status_code}, cost header {first.headers.get('x-prism-cost-usd')} (the key's whole budget is $0.00001)\nrequest 2: HTTP {second.status_code}\n{json.dumps(error)}"
    return Check(
        "Budget rejection",
        ok,
        f"first request {first.status_code}, second {second.status_code} {error.get('type', '')}".strip(),
        evidence,
    )


def check_failover(stack: Stack) -> Check:
    lines, ok = [], True
    try:
        stack.mock("alpha", mode="down")
        r = stack.chat("research", "What is a message queue?")
        down = (
            r.status_code == 200
            and r.headers["x-prism-provider"].startswith("beta/")
            and r.headers["x-prism-fallback"] == "true"
        )
        lines.append(
            f"alpha down        -> HTTP {r.status_code}, served by {r.headers.get('x-prism-provider')}, fallback {r.headers.get('x-prism-fallback')}  [{'ok' if down else 'FAIL'}]"
        )
        stack.mock("alpha", mode="ok")
        r = stack.chat("research", "What is a message queue?")
        back = (
            r.status_code == 200
            and r.headers["x-prism-provider"].startswith("alpha/")
            and r.headers["x-prism-fallback"] == "false"
        )
        lines.append(
            f"alpha restored    -> HTTP {r.status_code}, served by {r.headers.get('x-prism-provider')}, fallback {r.headers.get('x-prism-fallback')}  [{'ok' if back else 'FAIL'}]"
        )
        stack.mock("alpha", latency_ms=3000)
        t0 = time.monotonic()
        r = stack.chat("research", "What is a message queue?")
        took = time.monotonic() - t0
        slow = (
            r.status_code == 200
            and r.headers["x-prism-provider"].startswith("beta/")
            and r.headers["x-prism-fallback"] == "true"
            and took < 4.5
        )
        lines.append(
            f"alpha 3000 ms slow -> HTTP {r.status_code}, served by {r.headers.get('x-prism-provider')}, fallback {r.headers.get('x-prism-fallback')}, answered in {took:.2f} s (timeout 2 s)  [{'ok' if slow else 'FAIL'}]"
        )
        ok = down and back and slow
    finally:
        stack.mock("alpha", mode="ok", latency_ms=0, fail_rate=0)
    return Check(
        "Provider failover",
        ok,
        "down, recovery and slow provider all handled" if ok else "a failover step failed",
        "\n".join(lines),
    )


def check_load(stack: Stack) -> list[Check]:
    mark = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    total, limit = 30, RPM["free-tier"]
    _, out = stack.run_script(
        [
            "scripts/load_test.py",
            "--url",
            stack.base,
            "--key",
            KEY["free-tier"],
            "--model",
            "fast",
            "--requests",
            str(total),
            "--concurrency",
            "10",
            "--rpm-limit",
            str(limit),
        ],
        180,
    )
    load = parse_load(out)
    if load["accepted"] is None or load["cost_usd"] is None:
        return [
            Check("Load test: no over-admission", False, "no request succeeded or the output could not be read", out)
        ]
    checks = [judge_load(load, limit, total)]
    checks[0].evidence = out
    usage = stack.admin_get("/admin/usage", key=KEY["free-tier"])
    checks.append(judge_accounting(load, usage))
    logs = [
        row
        for row in stack.admin_get("/admin/logs", key=KEY["free-tier"], limit=100)["logs"]
        if row["status"] == "ok" and row["created_at"] > mark
    ]
    if logs:
        latency, upstream = describe([r["latency_ms"] for r in logs]), describe([r["upstream_ms"] for r in logs])
        overhead = describe([r["latency_ms"] - r["upstream_ms"] for r in logs])
        table = [f"{'':32} {'min':>6} {'median':>7} {'p95':>6} {'max':>6}   (ms, {latency['n']} accepted requests)"]
        for label, d in (
            ("gateway total (latency_ms)", latency),
            ("waiting on provider (upstream_ms)", upstream),
            ("gateway overhead (difference)", overhead),
        ):
            table.append(f"{label:32} {d['min']:>6.0f} {d['median']:>7.0f} {d['p95']:>6.0f} {d['max']:>6.0f}")
        table.append(
            f"{'load-test script, its own view':32} {'':>6} {load['client_avg_ms']:>7} {load['client_p95_ms']:>6}   (avg and p95; includes client-side start-up)"
        )
        checks.append(
            Check(
                "Latency summary",
                None,
                f"gateway median {latency['median']:.0f} ms total, {overhead['median']:.0f} ms overhead",
                "\n".join(table),
            )
        )
    return checks


def check_isolation(stack: Stack) -> Check:
    """The search key's cached answer must never reach another key, and cache entries must survive a restart."""
    first, again = stack.chat("search", PROMPT_X), stack.chat("search", PROMPT_X)
    dev_stack.restart_gateway(stack.procs, stack.port, stack.db_path)  # also clears the in-memory rate limiter
    owner = stack.chat("search", PROMPT_X)
    other = stack.chat("free-tier", PROMPT_X)
    other_again = stack.chat("free-tier", PROMPT_X)
    cost = float(other.headers.get("x-prism-cost-usd", "0") or 0)
    stats = {row["key"]: row["entries"] for row in stack.admin_get("/admin/cache/stats")["by_key"]}
    ok = (
        first.headers.get("x-prism-cache") == "miss" and again.headers.get("x-prism-cache") == "hit"
        and owner.headers.get("x-prism-cache") == "hit"  # the entry survived the restart
        and other.status_code == 200 and other.headers.get("x-prism-cache") == "miss" and cost > 0  # a real miss, really paid for
        and other_again.headers.get("x-prism-cache") == "hit"  # the second key then builds its own entry
    )  # fmt: skip
    lines = [
        f"search key, first ask            -> cache {first.headers.get('x-prism-cache')}",
        f"search key, repeat               -> cache {again.headers.get('x-prism-cache')}",
        "(gateway restarted: memory cleared, database kept)",
        f"search key, after the restart    -> cache {owner.headers.get('x-prism-cache')}   (the entry persisted)",
        f"free-tier key, identical prompt  -> HTTP {other.status_code}, cache {other.headers.get('x-prism-cache')}, cost {other.headers.get('x-prism-cost-usd')}, served by {other.headers.get('x-prism-provider')}",
        f"free-tier key, repeat            -> cache {other_again.headers.get('x-prism-cache')}   (its own entry now)",
        f"cache entries per key            -> search {stats.get(KEY['search'], 0)}, free-tier {stats.get(KEY['free-tier'], 0)}",
    ]
    return Check(
        "Cache tenant isolation and persistence",
        ok,
        "another key got a real miss, and the owner's entry survived a restart"
        if ok
        else "isolation or persistence failed",
        "\n".join(lines),
    )


# ---- the in-process evals ---------------------------------------------------------------------------------


def check_routing() -> Check:
    tiers = {"simple": "fast", "complex": "smart"}
    provided = routing_eval.load_cases(routing_eval.SETS["provided"])
    threshold, _ = routing_eval.best_length_threshold(provided)
    lines, beats = [], True
    for name in ("provided", "held-out"):
        rows = routing_eval.run(routing_eval.load_cases(routing_eval.SETS[name]), tiers, threshold)
        router = sum(r["actual"] == r["expected"] for r in rows)
        baseline = sum(r["baseline"] == r["expected"] for r in rows)
        beats = beats and router > baseline
        lines.append(
            f"{name:9} set ({len(rows)} cases): router {router}/{len(rows)} = {router / len(rows):.1%}   length-only baseline {baseline}/{len(rows)} = {baseline / len(rows):.1%}"
        )
        lines += [
            f"   MISS {r['id']}: expected {r['expected']}, got {r['actual']}: {r['prompt'][:80]!r}"
            for r in rows
            if r["actual"] != r["expected"]
        ]
    lines.append(
        f"(the baseline is 'smart if the prompt has at least {threshold} characters', the best threshold on the provided set)"
    )
    return Check(
        "Routing eval (auto)", beats, "the router beats the length-only baseline on both sets", "\n".join(lines)
    )


def check_cache_eval() -> Check:
    rows = cache_eval.score_pairs(cache_eval.load_pairs())
    summaries = [cache_eval.summarize(rows, t) for t in cache_eval.seed_thresholds()]
    lines = [f"threshold {s['threshold']:g}: paraphrases hit {s['paraphrase_hits']}/{s['paraphrase_pairs']} = {s['recall']:.1%}, false hits {s['false_hits']}/{s['near_miss_pairs']} = {s['false_hit_rate']:.1%}" + (f"  ({', '.join(s['false_hit_ids'])})" if s["false_hit_ids"] else "") for s in summaries]  # fmt: skip
    ok = summaries[0]["false_hits"] == 0
    return Check(
        "Cache matcher eval",
        ok,
        f"no false hits at the strictest threshold ({summaries[0]['threshold']:g})",
        "\n".join(lines),
    )


# ---- orchestration -------------------------------------------------------------------------------------------


def guarded(name: str, fn) -> list[Check]:
    try:
        result = fn()
        return result if isinstance(result, list) else [result]
    except Exception:  # noqa: BLE001  (deliberate: a crashed check is a failed check, with the reason in the report)
        return [Check(name, False, "the check crashed", traceback.format_exc())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-tests", action="store_true", help="do not run the pytest suite")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "VERIFICATION.md")
    parser.add_argument("--gateway-port", type=int, default=8080)
    args = parser.parse_args()

    try:
        dev_stack.ensure_ports_free(
            {"alpha": dev_stack.ALPHA_PORT, "beta": dev_stack.BETA_PORT, "gateway": args.gateway_port}
        )
    except dev_stack.StackError as err:
        print(f"Cannot start: {err}")
        return 1

    checks: list[Check] = []

    def record(new: list[Check]) -> None:
        for c in new:
            print(
                f"  {'PASS' if c.passed else 'info' if c.passed is None else 'FAIL'}  {c.name}: {c.summary}", flush=True
            )
        checks.extend(new)

    if not args.skip_tests:
        print("Running the test suite (about 15 s) ...", flush=True)
        record(guarded("Automated tests", check_unit_tests))

    db_path = Path(tempfile.mkdtemp(prefix="prism-verify-")) / "verify.db"
    print("Starting a fresh stack ...", flush=True)
    try:
        procs = dev_stack.start_stack(args.gateway_port, db_path=db_path, announce=lambda _msg: None)
    except dev_stack.StackError as err:
        print(f"Cannot start the stack: {err}")
        return 1
    stack = Stack(args.gateway_port, db_path)
    stack.procs = procs
    try:
        for name, fn in (
            ("Provided smoke test", lambda: check_smoke(stack)),
            ("Budget rejection", lambda: check_budget(stack)),
            ("Provider failover", lambda: check_failover(stack)),
            ("Load test", lambda: check_load(stack)),
            ("Cache tenant isolation and persistence", lambda: check_isolation(stack)),
        ):
            print(f"Running: {name} ...", flush=True)
            record(guarded(name, fn))
    finally:
        stack.client.close()
        dev_stack.stop_all(stack.procs)

    record(guarded("Routing eval (auto)", check_routing))
    record(guarded("Cache matcher eval", check_cache_eval))

    meta = {
        "when": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "os": platform.platform(),
        "python": platform.python_version(),
    }
    args.output.write_text(render_report(meta, checks), encoding="utf-8")
    shutil.rmtree(db_path.parent, ignore_errors=True)  # the private database was only for this run
    failed = [c for c in checks if c.passed is False]
    print(
        f"\nWrote {args.output}\n{'ALL GRADED CHECKS PASSED' if not failed else f'{len(failed)} CHECK(S) FAILED: ' + ', '.join(c.name for c in failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())