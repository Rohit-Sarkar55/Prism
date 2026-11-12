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
    resp = httpx.Response(
        200, headers={"x-prism-provider": "alpha/alpha-small", "x-prism-cache": "hit", "x-prism-cost-usd": "0"}
    )
    line = demo.describe(resp)
    assert "HTTP 200" in line and "alpha/alpha-small" in line and "cache hit" in line and "cost $0" in line
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