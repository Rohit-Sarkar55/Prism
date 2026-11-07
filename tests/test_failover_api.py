"""Failover as a caller sees it: through the real app, headers and request log included."""

from app.providers import ProviderError

SEARCH = "prism-sk-search-1a2b3c"
RESEARCH = "prism-sk-research-4d5e6f"
ADMIN = {"Authorization": "Bearer demo-admin-token"}
BODY = {"model": "fast", "messages": [{"role": "user", "content": "What is a load balancer?"}]}
BETA_SMALL_COST = 2_500  # 5 prompt * 100 nano + 5 completion * 400 nano (beta-small prices)


def auth(key=SEARCH):
    return {"Authorization": f"Bearer {key}"}


async def post(client, key=SEARCH, **overrides):
    return await client.post("/v1/chat/completions", json={**BODY, **overrides}, headers=auth(key))


async def latest_log(client, key=SEARCH):
    return (await client.get("/admin/logs", params={"key": key, "limit": 1}, headers=ADMIN)).json()["logs"][0]


async def test_provider_down_is_served_by_the_fallback_and_flagged(client, fakes):
    fakes["alpha"].fail_times = 99
    resp = await post(client)
    assert resp.status_code == 200
    assert resp.headers["x-prism-provider"] == "beta/beta-small"
    assert resp.headers["x-prism-fallback"] == "true"
    assert resp.headers["x-prism-cost-usd"] == "0.000002500"  # priced as beta-small, the model that served it
    entry = await latest_log(client)
    assert (entry["resolved_provider"], entry["fallback"], entry["retries"]) == ("beta", True, 3)
    assert entry["cost_nano"] == BETA_SMALL_COST


async def test_the_primary_serves_again_as_soon_as_it_recovers(client, fakes):
    fakes["alpha"].fail_times = 99
    assert (await post(client)).headers["x-prism-fallback"] == "true"
    fakes["alpha"].fail_times = 0  # alpha comes back
    resp = await post(client)
    assert resp.headers["x-prism-provider"] == "alpha/alpha-small"
    assert resp.headers["x-prism-fallback"] == "false"


async def test_a_rate_limited_provider_hands_over_immediately(client, fakes):
    fakes["alpha"].fail_times = 1
    fakes["alpha"].error = ProviderError("alpha", "rate_limited", status=429, transient=True, retry_after_s=5)
    resp = await post(client)
    assert resp.headers["x-prism-provider"] == "beta/beta-small" and resp.headers["x-prism-fallback"] == "true"
    assert len(fakes["alpha"].calls) == 1  # no retry against a provider that said "slow down"


async def test_repeated_failures_degrade_the_provider_and_it_is_skipped(client, fakes):
    fakes["alpha"].fail_times = 999
    for _ in range(2):
        await post(client)  # 3 failed attempts each: 6 in a row, so alpha is now degraded
    calls_before = len(fakes["alpha"].calls)
    resp = await post(client)
    assert len(fakes["alpha"].calls) == calls_before  # skipped: alpha was not even tried
    assert resp.headers["x-prism-fallback"] == "true" and (await latest_log(client))["retries"] == 0
    health = (await client.get("/admin/providers", headers=ADMIN)).json()["providers"]
    assert health["alpha"]["state"] == "degraded" and health["beta"]["state"] == "healthy"


async def test_smart_alias_fails_over_to_its_own_fallback_chain(client, fakes):
    fakes["alpha"].fail_times = 99
    resp = await post(client, RESEARCH, model="smart")
    assert resp.headers["x-prism-provider"] == "beta/beta-large"


async def test_all_providers_down_is_a_clean_502_and_the_attempts_are_logged(client, fakes):
    fakes["alpha"].fail_times = fakes["beta"].fail_times = 99
    resp = await post(client)
    assert resp.status_code == 502 and resp.json()["error"]["type"] == "upstream_error"
    assert "alpha" not in resp.text and "beta" not in resp.text
    entry = await latest_log(client)
    assert (entry["status"], entry["retries"]) == ("upstream_error", 5)


async def test_streaming_fails_over_before_the_first_chunk_with_the_header_set(client, fakes):
    fakes["alpha"].fail_times = 99
    resp = await client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=auth())
    assert resp.status_code == 200 and "text/event-stream" in resp.headers["content-type"]
    assert resp.headers["x-prism-provider"] == "beta/beta-small"
    assert resp.headers["x-prism-fallback"] == "true"
    assert resp.text.rstrip().endswith("data: [DONE]")
    assert (await latest_log(client))["cost_nano"] == BETA_SMALL_COST


async def test_provider_health_endpoint_is_admin_only(client):
    assert (await client.get("/admin/providers")).status_code == 401
    body = (await client.get("/admin/providers", headers=ADMIN)).json()["providers"]
    assert set(body) == {"alpha", "beta"} and body["alpha"]["state"] == "healthy"