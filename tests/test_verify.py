"""The verification tool's parsing, judging and report rendering, tested on REAL script output captured on the
author's Windows machine, so a change in the provided scripts' format cannot silently break the report."""

from tools import verify

SMOKE_OUTPUT = """
[1] Non-streaming completion + header contract
  PASS  returns 200  (got 200)
[6] Semantic cache: paraphrases (each pair soft; at least one pair must hit)
  PASS  pair A paraphrase is a cache hit  (hit)
  WARN  pair B paraphrase is a cache hit  (miss)
==================================================
  17 passed, 1 warnings, 0 failed
"""

LOAD_OUTPUT = """Bursting 30 requests at concurrency 10 ...

Burst finished in 0.4s
  accepted (200):      10
  rate limited (429):  20
  latency avg/p95 ms:  313 / 351

Client-side accounting totals - compare with your usage API for this key:
  accepted requests:   10
  prompt tokens:       120
  completion tokens:   260
  sum of cost headers: 0.000174 USD (10 of 10 had a numeric header)

Over-admission check OK: 10 accepted <= limit 10.
"""

LOAD_ALL_FAILED = """Bursting 30 requests at concurrency 10 ...

Burst finished in 2.3s
  accepted (200):      0
  rate limited (429):  20
  other outcomes:      10  ['502']
  latency avg/p95 ms:  0 / 0

FAIL: no request succeeded - is the gateway up and the key valid?

Over-admission check OK: 0 accepted <= limit 10.
"""

USAGE = {"requests": 10, "prompt_tokens": 120, "completion_tokens": 260, "cost_usd": 0.0001737, "cost_nano": 173_700}


def test_the_smoke_summary_line_is_parsed():
    assert verify.parse_smoke(SMOKE_OUTPUT) == {"passed": 17, "warnings": 1, "failed": 0}
    assert verify.parse_smoke("no summary here") == {}


def test_the_load_test_output_is_parsed():
    load = verify.parse_load(LOAD_OUTPUT)
    assert (load["accepted"], load["rate_limited"], load["other"]) == (10, 20, 0)
    assert (load["client_avg_ms"], load["client_p95_ms"]) == (313, 351)
    assert (load["requests"], load["prompt_tokens"], load["completion_tokens"]) == (10, 120, 260)
    assert load["cost_usd"] == 0.000174 and load["over_admission_ok"] is True


def test_a_load_test_where_every_request_failed_is_a_failure_even_though_its_own_check_says_ok():
    # The script's over-admission line passes trivially when nothing was admitted. We must not.
    load = verify.parse_load(LOAD_ALL_FAILED)
    assert load["other"] == 10 and load["accepted"] == 0 and load["over_admission_ok"] is True
    assert verify.judge_load(load, limit=10, total=30).passed is False


def test_the_load_judge_passes_the_real_run_and_catches_over_admission_and_missing_requests():
    load = verify.parse_load(LOAD_OUTPUT)
    assert verify.judge_load(load, limit=10, total=30).passed is True
    assert (
        verify.judge_load({**load, "accepted": 11, "rate_limited": 19}, limit=10, total=30).passed is False
    )  # over the limit
    assert (
        verify.judge_load({**load, "rate_limited": 15}, limit=10, total=30).passed is False
    )  # requests unaccounted for


def test_accounting_reconciles_a_matching_run_to_the_scripts_rounding():
    check = verify.judge_accounting(verify.parse_load(LOAD_OUTPUT), USAGE)
    assert check.passed is True and "MISMATCH" not in check.evidence and "173700 nano-USD" in check.evidence


def test_accounting_catches_any_difference_in_requests_tokens_or_cost():
    load = verify.parse_load(LOAD_OUTPUT)
    for field, bad in (("requests", 9), ("prompt_tokens", 121), ("completion_tokens", 259), ("cost_usd", 0.00018)):
        check = verify.judge_accounting(load, {**USAGE, field: bad})
        assert check.passed is False and "MISMATCH" in check.evidence, field


def test_percentiles_use_the_nearest_rank_method():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert (
        verify.percentile(values, 50) == 50
        and verify.percentile(values, 95) == 100
        and verify.percentile(values, 100) == 100
    )
    assert verify.percentile([7], 95) == 7
    summary = verify.describe([69, 71, 73, 73, 74, 75, 75, 76, 77, 75])
    assert (summary["n"], summary["min"], summary["max"], summary["median"]) == (
        10,
        69,
        77,
        74,
    )  # nearest rank: the 5th of 10


def test_the_report_has_a_summary_table_counts_graded_checks_and_keeps_info_separate():
    checks = [
        verify.Check("Smoke", True, "17 passed", "evidence one"),
        verify.Check("Load", False, "11 accepted of 10", "evidence two"),
        verify.Check("Latency", None, "median 75 ms"),
    ]
    report = verify.render_report({"when": "2026-11-12 10:00", "os": "Windows-11", "python": "3.14.7"}, checks)
    assert "| 1 | Smoke | **PASS** | 17 passed |" in report and "| 2 | Load | **FAIL** | 11 accepted of 10 |" in report
    assert "| 3 | Latency | **info** | median 75 ms |" in report
    assert "**1 of 2 graded checks passed.**" in report  # the informational row is not graded
    assert "```text\nevidence one\n```" in report and "evidence two" in report
    assert "Windows-11, Python 3.14.7" in report
    assert "What this does not show" in report and "mock providers" in report  # the limits are always stated


def test_the_tool_never_touches_the_users_own_database():
    source = (verify.PROJECT_ROOT / "tools" / "verify.py").read_text(encoding="utf-8")
    assert "tempfile.mkdtemp" in source and "db_path=db_path" in source
    assert "unlink" not in source and "fresh-db" not in source