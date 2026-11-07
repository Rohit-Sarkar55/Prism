"""One-command routing eval for the `auto` alias.

    python -m tools.routing_eval                 # provided set + held-out set, per-case table
    python -m tools.routing_eval --json          # the JSON shape from the evaluation guide (provided set)
    python -m tools.routing_eval --min-accuracy 0.8   # exit 1 if the provided set scores lower

It classifies every prompt (no upstream call) and compares with a length-only baseline. The
baseline is given its best shot: its character threshold is the one that scores highest on
the provided set (and that same threshold is then applied to the held-out set).
`expected_tier` is read here only to grade; app/difficulty.py never sees it.
"""

import argparse
import json
import sys
from pathlib import Path

from app.config import PROJECT_ROOT
from app.difficulty import classify
from app.seed import load_gateway_config

SETS = {
    "provided": PROJECT_ROOT / "data" / "routing_eval.jsonl",
    "held-out": PROJECT_ROOT / "data" / "routing_holdout.jsonl",
}


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def best_length_threshold(cases: list[dict]) -> tuple[int, float]:
    """The character count that, used as 'longer than this means smart', scores highest."""
    best = (0, 0.0)
    for threshold in sorted({len(c["prompt"]) for c in cases}) + [10**6]:
        acc = sum(("smart" if len(c["prompt"]) >= threshold else "fast") == c["expected_tier"] for c in cases) / len(
            cases
        )
        if acc > best[1]:
            best = (threshold, acc)
    return best


def run(cases: list[dict], tiers: dict[str, str], threshold: int) -> list[dict]:
    rows = []
    for case in cases:
        result = classify([{"role": "user", "content": case["prompt"]}])
        actual = tiers[result.tier]
        baseline = "smart" if len(case["prompt"]) >= threshold else "fast"
        rows.append({
            "id": case["id"], "expected": case["expected_tier"], "actual": actual, "reason": result.reason,
            "baseline": baseline, "trap": "trap" in case.get("note", "").lower(), "note": case.get("note", ""),
            "prompt": case["prompt"],
        })  # fmt: skip
    return rows


def pct(rows: list[dict], key: str) -> str:
    if not rows:
        return "n/a"
    correct = sum(r[key] == r["expected"] for r in rows)
    return f"{correct}/{len(rows)} = {correct / len(rows):.1%}"


def print_set(name: str, rows: list[dict], threshold: int, verbose: bool) -> None:
    print(f"\n=== {name} set: {len(rows)} cases ===")
    if verbose:
        for r in rows:
            mark = "ok  " if r["actual"] == r["expected"] else "MISS"
            print(f"  {mark} {r['id']}  expected {r['expected']:5}  got {r['actual']:5}  | {r['reason']}")
            if r["actual"] != r["expected"]:
                print(f"       prompt: {r['prompt'][:110]!r}  [{r['note']}]")
    traps = [r for r in rows if r["trap"]]
    print(f"  length-only baseline (smart if >= {threshold} chars): {pct(rows, 'baseline')}")
    print(f"  prism auto router:                                  {pct(rows, 'actual')}")
    if traps:
        print(
            f"  on the trap cases only ({len(traps)}):  baseline {pct(traps, 'baseline')}   router {pct(traps, 'actual')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--json", action="store_true", help="print the guide's JSON shape for the provided set and exit"
    )
    parser.add_argument("--quiet", action="store_true", help="summaries only, no per-case lines")
    parser.add_argument("--min-accuracy", type=float, help="exit 1 if the provided-set accuracy is below this")
    args = parser.parse_args()

    config = load_gateway_config(PROJECT_ROOT / "data" / "gateway_config.sample.json")
    tiers = config["model_aliases"]["auto"]["route_by_difficulty"]  # simple -> fast, complex -> smart
    tiers = {"simple": "fast" if tiers["simple"] == "fast" else tiers["simple"], "complex": tiers["complex"]}
    tiers = {tier: {"fast": "fast", "smart": "smart"}.get(alias, alias) for tier, alias in tiers.items()}

    provided = load_cases(SETS["provided"])
    threshold, _ = best_length_threshold(provided)
    rows = run(provided, tiers, threshold)
    accuracy = sum(r["actual"] == r["expected"] for r in rows) / len(rows)

    if args.json:
        cases = [{"id": r["id"], "expected": r["expected"], "actual": r["actual"], "reason": r["reason"]} for r in rows]
        print(json.dumps({"total": len(rows), "correct": sum(c["expected"] == c["actual"] for c in cases),
                          "accuracy": round(accuracy, 4), "cases": cases}, indent=2))  # fmt: skip
    else:
        print_set("provided", rows, threshold, verbose=not args.quiet)
        if SETS["held-out"].exists():
            held = load_cases(SETS["held-out"])
            print_set("held-out", run(held, tiers, threshold), threshold, verbose=not args.quiet)
            print("\n  (the held-out set was written before the classifier; see README for how it was used)")

    if args.min_accuracy is not None and accuracy < args.min_accuracy:
        print(f"\nFAIL: provided-set accuracy {accuracy:.0%} < required {args.min_accuracy:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())