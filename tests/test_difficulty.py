"""The difficulty classifier: general rules (written fresh, not copied from the eval sets),
regression floors on both eval sets, and a guard that the answer key is never read."""

import json
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.difficulty import (
    COMPLEX_THRESHOLD,
    classify,
    last_user_text,
    split_instruction,
)
from tools.routing_eval import best_length_threshold, load_cases


def tier(prompt: str) -> str:
    return classify([{"role": "user", "content": prompt}]).tier


# ---- length is not difficulty ------------------------------------------------------------


def test_a_long_input_with_a_trivial_question_stays_simple():
    data = "; ".join(f"item {i} costs {i * 3} dollars" for i in range(1, 30))
    assert len(data) > 500
    assert tier(f"Here is the price list: {data}. What does item 7 cost?") == "simple"


def test_a_very_short_hard_prompt_is_complex():
    assert tier("Prove that there are infinitely many primes.") == "complex"


# ---- payload is not instruction ----------------------------------------------------------


def test_jargon_inside_quoted_text_is_ignored():
    prompt = "Fix the typo in this sentence: 'The distributed consensus protocol uses replicaton and quorum voting.'"
    assert tier(prompt) == "simple"


def test_jargon_in_a_pasted_log_is_ignored():
    log = "; ".join(f"12:0{i} ERROR deadlock detected shard={i} latency_ms={i * 100}" for i in range(8))
    instruction, _, has_payload = split_instruction(f"Log follows. {log}. When did the first error happen?")
    assert "deadlock" not in instruction and has_payload
    assert tier(f"Log follows. {log}. When did the first error happen?") == "simple"


def test_vocabulary_alone_can_never_reach_the_threshold():
    result = classify(
        [{"role": "user", "content": "Notes: distributed consistency, quorum partition, replication lag, throughput."}]
    )
    assert result.score < COMPLEX_THRESHOLD and result.tier == "simple"


def test_vocabulary_plus_a_real_ask_does_count():
    assert tier("Why would a distributed cache with replication increase tail latency?") == "complex"


# ---- individual rules --------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Compare tabs and spaces for indentation.",
        "Design a URL shortener.",
        "Estimate how many tennis balls fit in a school bus.",
        "Derive the quadratic formula.",
        "What are the most likely causes of intermittent 502 errors?",
    ],
)
def test_clear_hard_asks_are_complex(prompt):
    assert tier(prompt) == "complex"


@pytest.mark.parametrize(
    "prompt",
    [
        "What is the capital of Peru?",
        "Translate 'thank you' into Italian.",
        "Convert 30 minutes to seconds.",
        "Extract the phone number from this text: 'Call us at 555-0134 today.'",
        "Rewrite this to sound friendlier: 'Your request has been denied.'",
        "Is 221 prime?",
    ],
)
def test_clear_easy_asks_are_simple(prompt):
    assert tier(prompt) == "simple"


def test_summarising_provided_text_is_easy_but_summarising_from_knowledge_is_not_discounted():
    assert (
        tier("Summarize this paragraph: 'The team shipped the feature on Monday and fixed two bugs on Tuesday.'")
        == "simple"
    )
    discounted = classify([{"role": "user", "content": "Summarize the pros and cons of remote work."}])
    assert ("summarising provided text", -2.0) not in discounted.matched and discounted.tier == "complex"


def test_a_long_multi_clause_instruction_adds_weight():
    short = classify([{"role": "user", "content": "List some fruits."}])
    long_ask = (
        "List some fruits, then group them by colour, then by season, then by the continent they originally came from,"
        " and then sort each group alphabetically."
    )
    assert len(long_ask.split()) >= 22
    assert ("long multi-clause instruction", 1.0) in classify([{"role": "user", "content": long_ask}]).matched
    assert short.score == 0


def test_reason_names_the_tier_the_score_and_the_signals():
    result = classify([{"role": "user", "content": "Prove that the square root of 3 is irrational."}])
    assert result.reason.startswith("complex (score 3):") and "proof or derivation +3" in result.reason


# ---- which message is judged -------------------------------------------------------------


def test_only_the_last_user_message_is_judged_and_system_prompts_are_ignored():
    messages = [
        {"role": "system", "content": "You are an expert distributed systems architect. Prove everything."},
        {"role": "user", "content": "Design a global payments platform."},
        {"role": "assistant", "content": "Sure..."},
        {"role": "user", "content": "What is the capital of Peru?"},
    ]
    assert classify(messages).tier == "simple"


def test_content_given_as_a_list_of_parts_is_read():
    parts = [
        {"type": "text", "text": "Prove that"},
        {"type": "image_url", "image_url": {"url": "x"}},
        {"type": "text", "text": "this holds."},
    ]
    assert "Prove that this holds." in last_user_text([{"role": "user", "content": parts}])


def test_no_text_is_simple_not_an_error():
    assert classify([{"role": "user", "content": ""}]).tier == "simple"
    assert classify([{"role": "system", "content": "only a system prompt"}]).tier == "simple"


# ---- honesty guards ----------------------------------------------------------------------


@pytest.mark.parametrize("module", ["app/difficulty.py", "app/routing.py"])
def test_the_router_never_reads_the_answer_key(module):
    source = (PROJECT_ROOT / module).read_text(encoding="utf-8")
    assert "expected_tier" not in source and "routing_eval" not in source and "routing_holdout" not in source


def test_classification_is_deterministic():
    messages = [{"role": "user", "content": "Compare event sourcing with CRUD and recommend one."}]
    assert len({classify(messages).reason for _ in range(20)}) == 1


# ---- regression floors on the eval sets --------------------------------------------------


def accuracy(path: Path) -> float:
    cases = load_cases(path)
    ok = sum({"simple": "fast", "complex": "smart"}[tier(c["prompt"])] == c["expected_tier"] for c in cases)
    return ok / len(cases)


def test_provided_set_stays_above_the_floor_and_beats_the_length_baseline():
    path = PROJECT_ROOT / "data" / "routing_eval.jsonl"
    _, baseline = best_length_threshold(load_cases(path))
    assert accuracy(path) >= 0.9
    assert accuracy(path) > baseline


def test_held_out_set_stays_above_the_floor_and_beats_the_length_baseline():
    provided_threshold, _ = best_length_threshold(load_cases(PROJECT_ROOT / "data" / "routing_eval.jsonl"))
    path = PROJECT_ROOT / "data" / "routing_holdout.jsonl"
    cases = load_cases(path)
    baseline = sum(
        ("smart" if len(c["prompt"]) >= provided_threshold else "fast") == c["expected_tier"] for c in cases
    ) / len(cases)
    assert accuracy(path) >= 0.8  # a floor against silent regressions, not a claim of independence
    assert accuracy(path) > baseline


def test_the_held_out_file_is_well_formed():
    cases = load_cases(PROJECT_ROOT / "data" / "routing_holdout.jsonl")
    assert len(cases) == 40 and len({c["id"] for c in cases}) == 40
    assert {c["expected_tier"] for c in cases} == {"fast", "smart"}
    assert all(json.dumps(c) for c in cases)


# ---- the eval tool itself ----------------------------------------------------------------


def test_best_length_threshold_finds_the_most_accurate_cut():
    cases = [
        {"prompt": "a" * 10, "expected_tier": "fast"},
        {"prompt": "a" * 20, "expected_tier": "fast"},
        {"prompt": "a" * 90, "expected_tier": "smart"},
        {"prompt": "a" * 100, "expected_tier": "smart"},
    ]
    assert best_length_threshold(cases) == (90, 1.0)