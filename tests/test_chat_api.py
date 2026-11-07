"""End-to-end through the real FastAPI app, with fake providers behind the registry."""

import asyncio

import pytest

from app.main import app

SEARCH = "prism-sk-search-1a2b3c"  # allowlist: fast
RESEARCH = "prism-sk-research-4d5e6f"  # allowlist: fast, smart, auto
ADMIN = {"Authorization": "Bearer demo-admin-token"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "What is a load balancer?"}]}
# FakeAdapter default: 5 prompt tokens, 5 reply words -> 5*150 + 5*600 nano on alpha-small
EXPECTED_COST_NANO = 3_750


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def logs(client, key=None, limit=50):
    params = {"limit": limit, **({"key": key} if key else {})}
    return (await client.get("/admin/logs", params=params, headers=ADMIN)).json()["logs"]


async def usage(client, key):
    return (await client.get("/admin/usage", params={"key": key}, headers=ADMIN)).json()


def assert_header_contract(resp):
    for name in ("x-prism-provider", "x-prism-cache", "x-prism-fallback", "x-prism-cost-usd"):
        assert name in resp.headers, f"missing {name}"


# ---- success path ----------------------------------------------------------------------


async def test_successful_completion_has_openai_body_and_full_header_contract(client):
    resp = await client.post("/v1/chat/completions", json=BODY, headers=auth(SEARCH))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["role"] == "assistant"
    assert resp.json()["usage"]["prompt_tokens"] == 5
    assert resp.headers["x-prism-provider"] == "alpha/alpha-small"
    assert resp.headers["x-prism-cache"] == "miss"
    assert resp.headers["x-prism-fallback"] == "false"
    assert resp.headers["x-prism-cost-usd"] == "0.000003750"


async def test_cost_is_logged_and_metered_from_provider_usage(client):
    await client.post("/v1/chat/completions", json=BODY, headers=auth(SEARCH))
    entry = (await logs(client, SEARCH))[0]
    assert entry["status"] == "ok" and entry["cache"] == "miss" and entry["fallback"] is False
    assert (entry["resolved_provider"], entry["resolved_model"]) == ("alpha", "alpha-small")
    assert (entry["prompt_tokens"], entry["completion_tokens"], entry["cost_nano"]) == (5, 5, EXPECTED_COST_NANO)
    assert entry["latency_ms"] is not None and entry["upstream_ms"] is not None
    totals = await usage(client, SEARCH)
    assert (totals["requests"], totals["cost_nano"], totals["prompt_tokens"]) == (1, EXPECTED_COST_NANO, 5)


async def test_usage_summary_matches_monthly_counter(client):
    for _ in range(3):
        await client.post("/v1/chat/completions", json=BODY, headers=auth(SEARCH))
    month = (await logs(client, SEARCH))[0]["created_at"][:7]
    monthly = await app.state.store.month_usage(SEARCH, month)
    totals = await usage(client, SEARCH)
    assert monthly["requests"] == totals["requests"] == 3
    assert monthly["cost_nano"] == totals["cost_nano"] == 3 * EXPECTED_COST_NANO


async def test_concurrent_requests_are_all_counted_exactly(client):
    n = 25
    responses = await asyncio.gather(
        *(client.post("/v1/chat/completions", json=BODY, headers=auth(RESEARCH)) for _ in range(n))
    )
    assert all(r.status_code == 200 for r in responses)
    totals = await usage(client, RESEARCH)
    assert totals["requests"] == n
    assert totals["cost_nano"] == n * EXPECTED_COST_NANO
    assert len(await logs(client, RESEARCH, limit=100)) == n
    client_side = sum(int(r.headers["x-prism-cost-usd"].replace(".", "")) for r in responses)
    assert client_side == totals["cost_nano"]  # what a load test would add up from headers


# ---- rejections ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers", [{}, auth("not-a-real-key"), {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}]
)
async def test_bad_or_missing_key_is_401_and_logged(client, headers):
    resp = await client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"
    assert_header_contract(resp)
    entry = (await logs(client))[0]
    assert entry["status"] == "rejected_auth" and entry["virtual_key"] is None


async def test_model_not_on_allowlist_is_403_and_costs_nothing(client, fakes):
    resp = await client.post("/v1/chat/completions", json={**BODY, "model": "smart"}, headers=auth(SEARCH))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "model_not_allowed"
    assert_header_contract(resp)
    assert (await logs(client, SEARCH))[0]["status"] == "rejected_allowlist"
    assert (await usage(client, SEARCH))["requests"] == 0
    assert fakes["alpha"].calls == []  # rejected before any upstream call


async def test_unknown_model_is_404(client):
    resp = await client.post("/v1/chat/completions", json={**BODY, "model": "no-such-model"}, headers=auth(SEARCH))
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found_error"
    assert (await logs(client, SEARCH))[0]["status"] == "rejected_not_found"


@pytest.mark.parametrize(
    "payload",
    [{"messages": BODY["messages"]}, {"model": "fast"}, {"model": "fast", "messages": []},
     {"model": "fast", "messages": ["hi"]}, {"model": 5, "messages": BODY["messages"]}],
)  # fmt: skip
async def test_malformed_body_is_400(client, payload):
    resp = await client.post("/v1/chat/completions", json=payload, headers=auth(SEARCH))
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


async def test_non_json_body_is_400(client):
    resp = await client.post("/v1/chat/completions", content=b"not json", headers=auth(SEARCH))
    assert resp.status_code == 400


async def test_upstream_failure_is_502_with_no_provider_details_and_no_charge(client, fakes):
    fakes["alpha"].fail_times = 1
    resp = await client.post("/v1/chat/completions", json=BODY, headers=auth(SEARCH))
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    assert "alpha" not in resp.text
    assert (await logs(client, SEARCH))[0]["status"] == "upstream_error"
    assert (await usage(client, SEARCH))["requests"] == 0


# ---- routing ---------------------------------------------------------------------------


async def test_smart_alias_routes_to_the_large_model(client):
    resp = await client.post("/v1/chat/completions", json={**BODY, "model": "smart"}, headers=auth(RESEARCH))
    assert resp.status_code == 200
    assert resp.headers["x-prism-provider"] == "alpha/alpha-large"


async def test_auto_alias_logs_a_route_reason(client):
    resp = await client.post("/v1/chat/completions", json={**BODY, "model": "auto"}, headers=auth(RESEARCH))
    assert resp.status_code == 200
    entry = (await logs(client, RESEARCH))[0]
    assert entry["requested_model"] == "auto" and entry["route_reason"]


async def test_optional_params_are_forwarded_and_unknown_ones_dropped(client, fakes):
    seen = {}
    original = fakes["alpha"].chat

    async def spy(model, messages, **params):
        seen.update(params)
        return await original(model, messages, **params)

    fakes["alpha"].chat = spy
    payload = {**BODY, "temperature": 0.2, "max_tokens": 50, "logit_bias": {"1": 5}}
    await client.post("/v1/chat/completions", json=payload, headers=auth(SEARCH))
    assert seen == {"temperature": 0.2, "max_tokens": 50}


# ---- admin plane -----------------------------------------------------------------------


async def test_admin_requires_the_admin_token(client):
    assert (await client.get("/admin/logs")).status_code == 401
    assert (
        await client.get("/admin/logs", headers=auth(SEARCH))
    ).status_code == 401  # a tenant key is not an admin key
    assert (await client.get("/admin/usage", params={"key": SEARCH})).status_code == 401


async def test_usage_for_unknown_key_is_404_and_bad_dates_are_400(client):
    assert (await client.get("/admin/usage", params={"key": "nope"}, headers=ADMIN)).status_code == 404
    bad = await client.get("/admin/usage", params={"key": SEARCH, "from": "yesterday"}, headers=ADMIN)
    assert bad.status_code == 400
    backwards = await client.get(
        "/admin/usage", params={"key": SEARCH, "from": "2026-12-01", "to": "2026-11-01"}, headers=ADMIN
    )
    assert backwards.status_code == 400


async def test_logs_are_newest_first_and_filterable_by_key(client):
    await client.post("/v1/chat/completions", json=BODY, headers=auth(SEARCH))
    await client.post("/v1/chat/completions", json={**BODY, "model": "smart"}, headers=auth(RESEARCH))
    everything = await logs(client)
    assert [e["virtual_key"] for e in everything[:2]] == [RESEARCH, SEARCH]
    assert {e["virtual_key"] for e in await logs(client, SEARCH)} == {SEARCH}