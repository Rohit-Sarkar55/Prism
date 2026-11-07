"""The cache matcher. The graded prompt pairs are read from the pack's own sample file."""

import json

import pytest

from app.config import get_settings
from app.embedding import (
    Embedding,
    LexicalEmbedder,
    is_time_sensitive,
    question_type,
    stem,
)

E = LexicalEmbedder()


def sim(a: str, b: str) -> float:
    return E.similarity(E.embed(a), E.embed(b))


def sample(request_id: str) -> str:
    for line in (get_settings().data_dir / "sample_requests.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item["id"] == request_id:
            return item["body"]["messages"][-1]["content"]
    raise KeyError(request_id)


# ---- the graded pairs: what this matcher does and does not catch ----------------------------------


def test_graded_pair_a_paraphrase_scores_above_the_strictest_seed_threshold():
    assert sim(sample("req_cache_a1"), sample("req_cache_a2")) >= 0.92  # the search key's threshold


def test_graded_near_miss_a3_is_far_below_both_seed_thresholds():
    assert sim(sample("req_cache_a1"), sample("req_cache_a3")) < 0.6  # password reset vs two-factor reset


def test_graded_pair_b_is_a_known_miss_because_it_shares_no_content_words():
    """KNOWN LIMITATION, pinned so it is noticed if it ever changes: 'refund policy' vs 'get my money back' needs
    real semantics (an embedding model). A word-based matcher cannot see it."""
    assert sim(sample("req_cache_b1"), sample("req_cache_b2")) < 0.85


def test_unrelated_prompts_score_low():
    assert sim("What is a load balancer?", "Compare TCP and UDP for game servers.") < 0.3


# ---- what counts as the same prompt ---------------------------------------------------------------


def test_identical_prompts_score_one():
    assert sim("What is a message queue?", "What is a message queue?") == 1.0


def test_case_punctuation_and_whitespace_do_not_matter():
    assert sim("What is a message queue?", "  what   IS a message-queue ... ") == 1.0


def test_frame_words_and_word_order_do_not_matter():
    assert (
        sim("How do I reset the password on my dashboard?", "How can I reset the dashboard password, please?") >= 0.92
    )


@pytest.mark.parametrize(
    ("word", "base"),
    [
        ("plans", "plan"),
        ("policies", "policy"),
        ("resetting", "reset"),
        ("passwords", "password"),
        ("configured", "configur"),
    ],
)
def test_light_stemming(word, base):
    assert stem(word) == stem(base) or stem(word) == base


# ---- what must NOT match: a wrong hit serves the wrong answer -------------------------------------


def test_identifiers_must_match_exactly():
    assert sim("Explain the refund for order 1234", "Explain the refund for order 5678") == 0.0
    assert sim("Explain the refund for order 1234", "Explain the refund for order 1234") == 1.0


def test_a_different_random_salt_means_a_different_prompt_but_the_same_salt_still_matches():
    """The provided scripts prepend '(abc12345)'. Another run's salt must never hit; the same run's paraphrase must."""
    assert sim("(abc12345) What is a load balancer?", "(ffe09876) What is a load balancer?") == 0.0
    assert sim("(abc12345) How do I reset my password?", "(abc12345) What are the steps to reset my password?") >= 0.92


def test_negation_is_content_not_a_stopword():
    assert sim("How do I reset my password?", "How do I not reset my password?") < 0.85
    assert sim("I can't reset my password", "I reset my password") < 0.85


def test_a_different_question_type_is_penalised_below_the_default_thresholds():
    assert sim("How do I reset my password?", "Why do I reset my password?") == pytest.approx(0.8)


def test_swapping_the_direction_of_a_conversion_is_not_a_match():
    assert sim("Convert USD to EUR", "Convert EUR to USD") == 0.0
    assert sim("Convert USD to EUR", "convert usd to eur please") >= 0.92


def test_directional_words_alone_do_not_block_a_normal_paraphrase():
    assert (
        sim("What are the steps to reset my password?", "Reset my password") >= 0.8
    )  # 'steps to reset': not a directed pair


def test_prompts_made_only_of_stopwords_match_only_when_identical():
    assert sim("What is it?", "What is it?") == 1.0
    assert sim("What is it?", "How is that?") == 0.0


def test_one_differing_word_lands_between_the_two_seed_thresholds():
    a = "How do I tune nginx reverse proxy timeout buffer upstream keepalive?"
    b = "How do I tune nginx reverse proxy timeout buffer upstream gzip?"
    assert 0.85 <= sim(a, b) < 0.92  # a hit for the free-tier key (0.85), a miss for the search key (0.92)


def test_a_negation_mismatch_blocks_a_match_even_in_a_long_prompt():
    # one extra word barely moves a cosine score in a long prompt, so negation must be a hard block
    assert sim("Is it safe to take ibuprofen with alcohol?", "Is it safe to take ibuprofen without alcohol?") == 0.0
    assert sim("Can I use the API without an account?", "Can I use the API with an account?") == 0.0


def test_negation_that_agrees_on_both_sides_still_matches():
    assert sim("How do I not lose my data?", "How can I not lose data?") >= 0.9


def test_the_direction_of_a_comparison_matters():
    assert sim("Is Python faster than Java?", "Is Java faster than Python?") == 0.0
    assert sim("Is Python faster than Java?", "is python faster than java") == 1.0


def test_before_and_after_keep_their_direction():
    assert sim("What runs before the tests?", "What runs after the tests?") == 0.0


# ---- representation and helpers -------------------------------------------------------------------


def test_the_stored_representation_round_trips():
    original = E.embed("(abc12345) Convert USD to EUR for order 77")
    restored = Embedding.from_json(original.to_json().encode())  # the database stores bytes
    assert restored == original and E.similarity(original, restored) == 1.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How do I reset it?", "how"), ("What are the steps to reset it?", "how"), ("Why is it slow?", "why"),
        ("When does it renew?", "when"), ("Is 91 prime?", "yesno"), ("What is a queue?", "what"),
        ("(abc123) Is 91 prime?", "yesno"), ("Explain queues.", "other"),
    ],
)  # fmt: skip
def test_question_types(text, expected):
    assert question_type(text) == expected


def test_time_sensitive_prompts_are_recognised_including_the_packs_example():
    assert is_time_sensitive(sample("req_no_cache"))
    for text in ("What is the weather in Paris?", "Show me the latest release notes", "What changed this week?"):
        assert is_time_sensitive(text)
    for text in ("What is a load balancer?", "How do I reset my password?", sample("req_cache_b1")):
        assert not is_time_sensitive(text)