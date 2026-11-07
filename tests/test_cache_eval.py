"""The cache eval tool: it must measure the matcher the gateway runs, and pin what it found."""

import json
import sys

from app.config import PROJECT_ROOT
from tools import cache_eval


def rows():
    return cache_eval.score_pairs(cache_eval.load_pairs())


def test_the_tool_measures_the_same_matcher_the_gateway_uses():
    source = (PROJECT_ROOT / "tools" / "cache_eval.py").read_text(encoding="utf-8")
    assert "from app.embedding import LexicalEmbedder" in source
    assert "similarity import" not in source  # no second, different matcher


def test_the_thresholds_come_from_the_seed_data_not_a_constant():
    assert cache_eval.seed_thresholds() == [0.92, 0.85]


def test_the_pairs_file_is_well_formed():
    pairs = cache_eval.load_pairs()
    assert len({p["id"] for p in pairs}) == len(pairs)
    assert {p["kind"] for p in pairs} == {"paraphrase", "near_miss"}
    assert sum(p["kind"] == "paraphrase" for p in pairs) == 26 and sum(p["kind"] == "near_miss" for p in pairs) == 26


def test_the_graded_pairs_behave_at_both_seed_thresholds():
    by_id = {r["id"]: r for r in rows()}
    assert by_id["req_cache_a1/a2"]["sim"] >= 0.92  # the pair the brief says must hit
    assert by_id["req_cache_a1/a3"]["sim"] < 0.85  # the near-miss that must not
    assert by_id["req_cache_b1/b2"]["sim"] < 0.85  # pair B is the documented limit of a lexical matcher


def test_no_false_hits_at_the_strict_threshold():
    summary = cache_eval.summarize(rows(), 0.92)
    assert summary["false_hits"] == 0 and summary["false_hit_ids"] == []


def test_the_only_false_hits_at_the_looser_threshold_are_the_two_documented_ones():
    # Long prompts where ONE critical word differs score about 0.89: a hit at 0.85, a miss at 0.92.
    # That is the known limit of a content-word matcher. If this set ever changes, update the README.
    summary = cache_eval.summarize(rows(), 0.85)
    assert set(summary["false_hit_ids"]) == {"n08", "n25"}


def test_recall_is_low_and_that_is_stated_honestly():
    # No synonyms means most true paraphrases miss. That costs money, not correctness.
    summary = cache_eval.summarize(rows(), 0.92)
    assert 0.05 <= summary["recall"] <= 0.5
    assert "req_cache_b1/b2" in summary["missed_paraphrases"]


def test_json_output_has_every_pair_and_a_summary_per_threshold(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cache_eval", "--json"])
    assert cache_eval.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["pairs"]) == 52 and [s["threshold"] for s in report["summaries"]] == [0.92, 0.85]


def test_a_single_threshold_can_be_requested(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cache_eval", "--threshold", "0.95", "--quiet"])
    assert cache_eval.main() == 0
    out = capsys.readouterr().out
    assert "threshold 0.95" in out and "threshold 0.85" not in out