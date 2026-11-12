"""The README makes many factual claims about the code. This test fails when one stops being true,
so the documentation cannot silently drift from the gateway."""

import json
import re

import pytest

from app import errors
from app.config import PROJECT_ROOT, Settings
from app.health import ProviderHealth
from app.main import app
from app.seed import load_gateway_config
from app.upstream import RetryPolicy
from tools import cache_eval, routing_eval

README = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
SEED = json.loads((PROJECT_ROOT / "data" / "seed_keys.json").read_text(encoding="utf-8"))["tenants"]


def table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|")]


# ---- endpoints, settings, files ---------------------------------------------------------------------


def test_every_route_the_readme_mentions_exists():
    routes = set(app.openapi()["paths"]) | {"/console"}  # the console page is excluded from the schema on purpose
    mentioned = set(re.findall(r"`(?:GET |POST |DELETE )?(/(?:v1|admin|health|console)[^\s`?]*)", README))
    assert mentioned, "the README mentions no routes?"
    assert mentioned <= routes, f"documented but missing: {sorted(mentioned - routes)}"


def test_every_route_the_gateway_serves_is_documented():
    undocumented = [p for p in app.openapi()["paths"] if p not in README]
    assert not undocumented, f"served but not in the README: {undocumented}"


def test_every_setting_is_documented_and_every_documented_setting_exists():
    real = {f"PRISM_{name.upper()}" for name in Settings.model_fields}
    assert set(re.findall(r"PRISM_[A-Z_]+", README)) == real


def test_every_file_the_readme_names_exists():
    paths = set(re.findall(r"`((?:app|tests|tools|data|scripts)/[\w./-]*)`", README))
    assert paths
    missing = [p for p in sorted(paths) if not (PROJECT_ROOT / p).exists()]
    assert not missing, f"named in the README but missing: {missing}"


def test_every_response_header_is_documented():
    for header in ("x-prism-provider", "x-prism-cache", "x-prism-fallback", "x-prism-cost-usd", "x-prism-request-id"):
        assert f"`{header}`" in README


# ---- seeded keys ----------------------------------------------------------------------------------


def money(usd: float) -> str:
    return f"${usd:.5f}".rstrip("0").rstrip(".") if usd < 1 else f"${usd:g}"


def test_the_demo_key_table_matches_the_seed_data():
    rows = {key: row for row in table_rows(README) for key in re.findall(r"prism-sk-[\w-]+", row)}
    assert set(rows) == {t["virtual_key"] for t in SEED}  # no key missing, no invented key
    for tenant in SEED:
        row = rows[tenant["virtual_key"]]
        assert f"{tenant['rate_limit']['requests_per_minute']} rpm" in row
        assert money(tenant["monthly_budget_usd"]) in row
        assert all(model in row for model in tenant["model_allowlist"])
        cache = tenant["semantic_cache"]
        assert (
            (f"threshold {cache['similarity_threshold']}" in row)
            if cache["enabled"]
            else row.rstrip().endswith("off |")
        )


# ---- the error table ------------------------------------------------------------------------------


def test_the_error_table_matches_the_code():
    rows = table_rows(README)
    cases = [
        errors.authentication_error(), errors.invalid_request("x"), errors.not_found("m"), errors.model_not_allowed("m"),
        errors.rate_limited(10, 1.0), errors.budget_exhausted(10_000), errors.upstream_failed(),
    ]  # fmt: skip
    for err in cases:
        row = next((r for r in rows if f"`{err.type}`" in r), None)
        assert row is not None, f"{err.type} is not in the error table"
        assert f"| {err.status} |" in row and f"`{err.log_status}`" in row


# ---- numbers quoted in the prose ---------------------------------------------------------------------


def test_the_documented_defaults_match_the_code():
    retry, health, settings = RetryPolicy(), ProviderHealth(), Settings()
    config = load_gateway_config(PROJECT_ROOT / "data" / "gateway_config.sample.json")
    assert (retry.max_attempts, retry.initial_backoff_ms, retry.backoff_multiplier) == (3, 200, 2.0)
    assert config["retry"] == {"max_attempts": 3, "initial_backoff_ms": 200, "backoff_multiplier": 2}
    assert (health.max_consecutive_failures, health.min_samples, health.probe_interval_s, health.window_s) == (
        5,
        10,
        5.0,
        60,
    )
    assert health.error_rate_threshold == 0.5
    assert (settings.provider_timeout_s, settings.upstream_deadline_s) == (2.0, 10.0)
    assert (settings.cache_ttl_s, settings.cache_max_entries_per_key) == (86_400, 500)
    for phrase in ("up to 3 attempts", "200 ms then 400 ms", "(2 s, connect 1 s)", "overall deadline (10 s)",
                   "5 consecutive failures", "at least 10 calls in 60 seconds", "every 5 seconds",
                   "expire after 24 hours", "at most 500", "over 4000 characters"):  # fmt: skip
        assert phrase in README, phrase
    assert "4000" in (PROJECT_ROOT / "app" / "cache.py").read_text(encoding="utf-8")


def test_the_router_threshold_in_the_readme_is_the_real_one():
    from app.difficulty import COMPLEX_THRESHOLD

    assert COMPLEX_THRESHOLD == 2.0 and "A score of 2.0 or more" in README


@pytest.mark.parametrize("which", ["provided", "held-out"])
def test_the_routing_numbers_in_the_readme_are_what_the_eval_computes(which):
    config = load_gateway_config(PROJECT_ROOT / "data" / "gateway_config.sample.json")
    tiers = {"simple": "fast", "complex": "smart"}
    assert config["model_aliases"]["auto"]["route_by_difficulty"] == tiers
    cases = routing_eval.load_cases(routing_eval.SETS[which])
    threshold, _ = routing_eval.best_length_threshold(routing_eval.load_cases(routing_eval.SETS["provided"]))
    rows = routing_eval.run(cases, tiers, threshold)
    router = sum(r["actual"] == r["expected"] for r in rows) / len(rows)
    baseline = sum(r["baseline"] == r["expected"] for r in rows) / len(rows)
    expected = {"provided": (1.0, 0.75), "held-out": (0.875, 0.65)}[which]
    assert (router, baseline) == expected
    for value in expected:
        assert f"{value:.0%}" in README or f"{value:.1%}" in README


def test_the_cache_numbers_in_the_readme_are_what_the_eval_computes():
    rows = cache_eval.score_pairs(cache_eval.load_pairs())
    strict, loose = cache_eval.summarize(rows, 0.92), cache_eval.summarize(rows, 0.85)
    assert (strict["recall"], strict["false_hits"], loose["recall"], loose["false_hits"]) == (3 / 26, 0, 5 / 26, 2)
    for phrase in (
        "26 paraphrase and 26 near-miss pairs",
        "11.5% of paraphrases hit, 0 false hits",
        "19.2%\n  hit, 2 false hits",
    ):
        assert phrase in README, phrase