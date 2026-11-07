"""One-command cache matcher eval: does the similarity decision match the labels?

    python -m tools.cache_eval                     # every threshold the seed data uses
    python -m tools.cache_eval --threshold 0.9     # one threshold
    python -m tools.cache_eval --json

It scores labeled prompt pairs with the SAME matcher the gateway uses (app/embedding.py) and
reports, per threshold:
  * recall          - paraphrase pairs that hit (a miss only costs money)
  * false-hit rate  - near-miss pairs that hit (a false hit serves a WRONG answer: the number that matters)
The pairs are in data/cache_pairs_holdout.jsonl plus the three pairs from the provided sample requests.
"""

import argparse
import json
import sys

from app.config import PROJECT_ROOT
from app.embedding import LexicalEmbedder

PAIRS = PROJECT_ROOT / "data" / "cache_pairs_holdout.jsonl"
SEED_KEYS = PROJECT_ROOT / "data" / "seed_keys.json"
SAMPLE_PAIRS = [  # from data/sample_requests.jsonl
    {"id": "req_cache_a1/a2", "kind": "paraphrase", "note": "provided pair A", "a": "How do I reset my password on the dashboard?", "b": "What are the steps to reset my dashboard password?"},
    {"id": "req_cache_a1/a3", "kind": "near_miss", "note": "provided near-miss", "a": "How do I reset my password on the dashboard?", "b": "How do I reset my two-factor authentication on the dashboard?"},
    {"id": "req_cache_b1/b2", "kind": "paraphrase", "note": "provided pair B", "a": "What is the refund policy for annual plans?", "b": "If I bought an annual plan, can I get my money back?"},
]  # fmt: skip


def seed_thresholds() -> list[float]:
    tenants = json.loads(SEED_KEYS.read_text(encoding="utf-8"))["tenants"]
    return sorted(
        {t["semantic_cache"]["similarity_threshold"] for t in tenants if t["semantic_cache"].get("enabled")},
        reverse=True,
    )


def load_pairs() -> list[dict]:
    rows = [json.loads(line) for line in PAIRS.read_text(encoding="utf-8").splitlines() if line.strip()]
    return SAMPLE_PAIRS + rows


def score_pairs(pairs: list[dict]) -> list[dict]:
    embedder = LexicalEmbedder()
    return [{**p, "sim": embedder.similarity(embedder.embed(p["a"]), embedder.embed(p["b"]))} for p in pairs]


def summarize(rows: list[dict], threshold: float) -> dict:
    para = [r for r in rows if r["kind"] == "paraphrase"]
    near = [r for r in rows if r["kind"] == "near_miss"]
    hits = [r for r in para if r["sim"] >= threshold]
    false_hits = [r for r in near if r["sim"] >= threshold]
    return {
        "threshold": threshold,
        "paraphrase_pairs": len(para), "paraphrase_hits": len(hits), "recall": len(hits) / len(para),
        "near_miss_pairs": len(near), "false_hits": len(false_hits), "false_hit_rate": len(false_hits) / len(near),
        "missed_paraphrases": [r["id"] for r in para if r["sim"] < threshold],
        "false_hit_ids": [r["id"] for r in false_hits],
    }  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threshold", type=float, help="evaluate one threshold instead of the seed data's")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="summaries only, no per-pair lines")
    args = parser.parse_args()

    rows = score_pairs(load_pairs())
    thresholds = [args.threshold] if args.threshold is not None else seed_thresholds()
    summaries = [summarize(rows, t) for t in thresholds]

    if args.json:
        json.dump({"pairs": rows, "summaries": summaries}, sys.stdout, indent=2)
        print()
        return 0

    if not args.quiet:
        marks = " ".join(f"@{t:g}" for t in thresholds)
        print(f"{'id':16} {'kind':10} {'sim':>5}  decision {marks}   note")
        for r in rows:
            decisions = " ".join(("HIT " if r["sim"] >= t else "miss") + " " * (len(f"@{t:g}") - 3) for t in thresholds)
            wrong = any((r["sim"] >= t) != (r["kind"] == "paraphrase") for t in thresholds)
            print(
                f"{r['id']:16} {r['kind']:10} {r['sim']:5.2f}  {decisions}  {'<-- ' if wrong else '    '}{r.get('note', '')}"
            )
    for s in summaries:
        print(f"\nthreshold {s['threshold']:g}:")
        print(
            f"  paraphrases that hit : {s['paraphrase_hits']}/{s['paraphrase_pairs']} = {s['recall']:.1%}   (misses cost money, not correctness)"
        )
        print(
            f"  FALSE hits           : {s['false_hits']}/{s['near_miss_pairs']} = {s['false_hit_rate']:.1%}   (serve a wrong answer)"
        )
        if s["false_hit_ids"]:
            print(f"  false hits are       : {', '.join(s['false_hit_ids'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())