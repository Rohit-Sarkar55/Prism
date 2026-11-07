"""Rate limit and budget, end to end through the real app."""

import asyncio

import pytest

from app.main import app

SEARCH = "prism-sk-search-1a2b3c"  # rpm 60, allowlist: fast
FREE = "prism-sk-free-7g8h9i"  # rpm 10, allowlist: fast
BUDGET_DEMO = "prism-sk-budget-demo-0j1k2l"  # budget $0.00001 = 10_000 nano-USD
ADMIN = {"Authorization": "Bearer demo-admin-token"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "What is a load balancer?"}]}
BUDGET_DEMO_NANO = 10_000
COST = 3_750  # default FakeAdapter reply: 5 prompt tokens, 5 completion tokens on alpha-small


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def post(client, key, **overrides):
    return await client.post("/v1/chat/completions", json={**BODY, **overrides}, headers=auth(key))


async def logs(client, key):
    return (await client.get("/admin/logs", params={"key": key, "limit": 100}, headers=ADMIN)).json()["logs"]


async def usage(client, key):
    return (await client.get("/admin/usage", params={"key": key}, headers=ADMIN)).json()


# ---- rate limit ------------------------------------------------------------------------


async def test_requests_past_the_per_minute_limit_get_429_with_retry_after(client, fakes):
    statuses = [(await post(client, FREE)).status_code for _ in range(12)]
    assert statuses == [200] * 10 + [429] * 2
    limited = await post(client, FREE)
    assert limited.json()["error"]["type"] == "rate_limit_exceeded"
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    for name in ("x-prism-provider", "x-prism-cache", "x-prism-fallback", "x-prism-cost-usd"):
        assert name in limited.headers
    assert len(fakes["alpha"].calls) == 10  # rejected requests never reached the provider
    assert (await usage(client, FREE))["requests"] == 10
    rejected = [e for e in await logs(client, FREE) if e["status"] == "rejected_rate_limit"]
    assert len(rejected) == 3 and rejected[0]["requested_model"] == "fast"


async def test_one_keys_limit_does_not_affect_another(client):
    for _ in range(11):
        await post(client, FREE)
    assert (await post(client, FREE)).status_code == 429
    assert (await post(client, SEARCH)).status_code == 200


async def test_concurrent_burst_admits_exactly_the_limit(client):
    """The same shape as scripts/load_test.py: 30 concurrent requests against a 10 rpm key."""
    responses = await asyncio.gather(*(post(client, FREE) for _ in range(30)))
    accepted = [r for r in responses if r.status_code == 200]
    assert len(accepted) == 10  # zero over-admission
    assert sum(r.status_code == 429 for r in responses) == 20
    totals = await usage(client, FREE)
    assert (totals["requests"], totals["cost_nano"]) == (10, 10 * COST)


async def test_allowlist_rejections_do_not_use_up_rate_limit_slots(client):
    for _ in range(15):
        assert (await post(client, FREE, model="smart")).status_code == 403
    assert [(await post(client, FREE)).status_code for _ in range(11)] == [200] * 10 + [429]


# ---- budget ----------------------------------------------------------------------------


@pytest.fixture
def expensive(fakes):
    # 5 prompt + 20 completion tokens = 750 + 12_000 = 12_750 nano, more than the 10_000 budget
    fakes["alpha"].reply = " ".join(["word"] * 20)


async def test_budget_demo_key_gets_one_request_then_budget_exceeded(client, expensive):
    first = await post(client, BUDGET_DEMO)
    assert first.status_code == 200
    second = await post(client, BUDGET_DEMO)
    assert second.status_code == 402
    error = second.json()["error"]
    assert error["type"] == "budget_exceeded"
    assert error["message"] == "Monthly budget of $0.00001 exhausted for this key"
    assert "x-prism-cost-usd" in second.headers
    assert [e["status"] for e in await logs(client, BUDGET_DEMO)] == ["rejected_budget", "ok"]  # rejection is logged
    assert (await usage(client, BUDGET_DEMO))["requests"] == 1


async def test_a_request_is_admitted_while_budget_remains_even_if_it_overshoots(client, expensive):
    await post(client, BUDGET_DEMO)
    spent = (await usage(client, BUDGET_DEMO))["cost_nano"]
    assert spent == 12_750
    assert spent > BUDGET_DEMO_NANO  # documented overshoot: the cost is only known afterwards


async def test_cheap_requests_keep_going_until_the_budget_is_actually_spent(client):
    # 3_750 nano per request against a 10_000 budget: spent 0, 3750, 7500 are all < 10_000, then 11_250 is not
    statuses = [(await post(client, BUDGET_DEMO)).status_code for _ in range(5)]
    assert statuses == [200, 200, 200, 402, 402]


async def test_budget_also_blocks_streaming_requests(client, expensive):
    await post(client, BUDGET_DEMO)
    resp = await client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=auth(BUDGET_DEMO))
    assert resp.status_code == 402
    assert "text/event-stream" not in resp.headers["content-type"]


async def test_budget_resets_with_the_calendar_month(client):
    # Heavy spending last month must not count this month: usage is keyed by month.
    await app.state.db.execute(
        "INSERT INTO usage_monthly (virtual_key, month, requests, cost_nano) VALUES (?, '2000-01', 1, 999999999999)",
        (BUDGET_DEMO,),
    )
    await app.state.db.commit()
    assert (await post(client, BUDGET_DEMO)).status_code == 200


async def test_rate_limit_is_checked_before_budget(client, expensive):
    await post(client, BUDGET_DEMO)  # exhausts the budget
    # the budget-demo key allows 60 rpm, so this is a budget rejection, not a rate-limit one
    assert (await post(client, BUDGET_DEMO)).json()["error"]["type"] == "budget_exceeded"


async def test_a_key_with_a_big_budget_keeps_working(client):
    for _ in range(5):
        assert (await post(client, SEARCH)).status_code == 200