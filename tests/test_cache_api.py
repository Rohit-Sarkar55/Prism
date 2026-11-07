"""The semantic cache, end to end through the real app (cache on for the search and free-tier keys, as seeded)."""

import asyncio
import json

import httpx

from app.health import ProviderHealth
from app.main import app
from app.providers import ProviderRegistry
from app.providers.fake import FakeAdapter
from app.upstream import RetryPolicy, Upstream

SEARCH = "prism-sk-search-1a2b3c"  # cache on, threshold 0.92, rpm 60, allowlist: fast
FREE = "prism-sk-free-7g8h9i"  # cache on, threshold 0.85, rpm 10, allowlist: fast
RESEARCH = "prism-sk-research-4d5e6f"  # cache OFF in the seed; allowlist: fast, smart, auto
BUDGET_DEMO = "prism-sk-budget-demo-0j1k2l"  # cache OFF in the seed; budget $0.00001
ADMIN = {"Authorization": "Bearer demo-admin-token"}
COST = 3_750  # default FakeAdapter reply: 5 prompt + 5 completion tokens on alpha-small
REPLY = "hello from the fake provider"

A1, A2 = "How do I reset my password on the dashboard?", "What are the steps to reset my dashboard password?"
A3 = "How do I reset my two-factor authentication on the dashboard?"
B1, B2 = "What is the refund policy for annual plans?", "If I bought an annual plan, can I get my money back?"


async def noop_sleep(_seconds):
    return None


def auth(key):
    return {"Authorization": f"Bearer {key}"}


async def ask(client, key, prompt, model="fast", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}
    return await client.post("/v1/chat/completions", json=body, headers=auth(key))


async def ask_messages(client, key, messages, model="fast"):
    return await client.post("/v1/chat/completions", json={"model": model, "messages": messages}, headers=auth(key))


async def enable_cache(key, threshold=0.9):
    await app.state.db.execute(
        "UPDATE virtual_keys SET cache_enabled = 1, cache_threshold = ? WHERE virtual_key = ?", (threshold, key)
    )
    await app.state.db.commit()


async def logs(client, key, limit=50):
    return (await client.get("/admin/logs", params={"key": key, "limit": limit}, headers=ADMIN)).json()["logs"]


async def usage(client, key):
    return (await client.get("/admin/usage", params={"key": key}, headers=ADMIN)).json()


async def stats(client, key=None):
    params = {"key": key} if key else {}
    return (await client.get("/admin/cache/stats", params=params, headers=ADMIN)).json()


def verdict(resp):
    return resp.headers["x-prism-cache"]


def sse_lines(text):
    return [line for line in text.splitlines() if line.startswith("data: ")]


# ---- hits and misses ------------------------------------------------------------------------------


async def test_an_exact_repeat_is_served_from_cache_at_zero_cost(cache_client, fakes):
    first = await ask(cache_client, SEARCH, "What is a message queue?")
    second = await ask(cache_client, SEARCH, "What is a message queue?")

    assert (verdict(first), verdict(second)) == ("miss", "hit")
    assert second.headers["x-prism-provider"] == "cache"
    assert second.headers["x-prism-cost-usd"] == "0" and second.headers["x-prism-fallback"] == "false"
    assert second.json()["choices"][0]["message"]["content"] == first.json()["choices"][0]["message"]["content"]
    assert second.json()["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert len(fakes["alpha"].calls) == 1  # the second request never reached a provider

    entry = (await logs(cache_client, SEARCH))[0]
    assert (entry["status"], entry["cache"], entry["resolved_provider"], entry["cost_nano"]) == (
        "cache_hit",
        "hit",
        None,
        0,
    )
    assert entry["cache_similarity"] == 1.0 and "cache hit" in entry["route_reason"]
    totals = await usage(cache_client, SEARCH)
    assert (totals["requests"], totals["cache_hits"], totals["cost_nano"]) == (2, 1, COST)


async def test_graded_pair_a_hits_the_near_miss_does_not_and_pair_b_is_the_documented_limit(cache_client):
    assert verdict(await ask(cache_client, SEARCH, A1)) == "miss"
    assert verdict(await ask(cache_client, SEARCH, A2)) == "hit"  # the paraphrase, at the strict 0.92 threshold
    assert verdict(await ask(cache_client, SEARCH, A3)) == "miss"  # similar words, different intent
    assert verdict(await ask(cache_client, SEARCH, B1)) == "miss"
    assert verdict(await ask(cache_client, SEARCH, B2)) == "miss"  # needs real semantics: see test_embedding.py


async def test_the_threshold_is_per_key(cache_client):
    a = "How do I tune nginx reverse proxy timeout buffer upstream keepalive?"
    b = "How do I tune nginx reverse proxy timeout buffer upstream gzip?"  # similarity 0.875
    for key, expected in ((FREE, "hit"), (SEARCH, "miss")):  # free-tier threshold 0.85, search 0.92
        await ask(cache_client, key, a)
        assert verdict(await ask(cache_client, key, b)) == expected, key


async def test_a_different_id_or_salt_is_never_a_hit_but_the_same_salt_is(cache_client):
    await ask(cache_client, SEARCH, "(abc12345) How do I reset my password?")
    assert verdict(await ask(cache_client, SEARCH, "(ffe09876) How do I reset my password?")) == "miss"
    assert verdict(await ask(cache_client, SEARCH, "(abc12345) What are the steps to reset my password?")) == "hit"
    await ask(cache_client, SEARCH, "Explain the refund for order 1234")
    assert verdict(await ask(cache_client, SEARCH, "Explain the refund for order 5678")) == "miss"


# ---- scope: where a hit is not allowed to cross ---------------------------------------------------


async def test_the_cache_is_never_shared_between_keys(cache_client, fakes):
    prompt = "Summarise our internal pricing strategy"
    assert verdict(await ask(cache_client, SEARCH, prompt)) == "miss"
    assert verdict(await ask(cache_client, FREE, prompt)) == "miss"  # same prompt, other tenant: must NOT hit
    assert len(fakes["alpha"].calls) == 2
    assert verdict(await ask(cache_client, SEARCH, prompt)) == "hit"  # but each tenant has its own entry
    assert verdict(await ask(cache_client, FREE, prompt)) == "hit"
    by_key = {row["key"]: row["entries"] for row in (await stats(cache_client))["by_key"]}
    assert by_key[SEARCH] == by_key[FREE] == 1


async def test_a_cache_hit_never_leaks_across_keys_even_for_a_paraphrase(cache_client):
    await ask(cache_client, SEARCH, A1)
    assert verdict(await ask(cache_client, FREE, A2)) == "miss"  # a paraphrase of another tenant's prompt


async def test_the_alias_is_part_of_the_scope(cache_client):
    await enable_cache(RESEARCH)
    prompt = "Describe the billing service"
    for model in ("fast", "smart", "auto"):
        assert verdict(await ask(cache_client, RESEARCH, prompt, model=model)) == "miss", model
    for model in ("fast", "smart", "auto"):
        assert verdict(await ask(cache_client, RESEARCH, prompt, model=model)) == "hit", model


async def test_request_parameters_are_part_of_the_scope(cache_client):
    await ask(cache_client, SEARCH, "Describe the export service", max_tokens=10)
    assert verdict(await ask(cache_client, SEARCH, "Describe the export service", max_tokens=500)) == "miss"
    assert verdict(await ask(cache_client, SEARCH, "Describe the export service")) == "miss"
    assert verdict(await ask(cache_client, SEARCH, "Describe the export service", max_tokens=10)) == "hit"


async def test_the_system_prompt_and_earlier_turns_must_match_exactly(cache_client):
    question = {"role": "user", "content": "Describe the upload service"}

    def system(text):
        return {"role": "system", "content": text}

    assert verdict(await ask_messages(cache_client, SEARCH, [system("Be terse."), question])) == "miss"
    assert verdict(await ask_messages(cache_client, SEARCH, [system("Be verbose."), question])) == "miss"
    assert verdict(await ask_messages(cache_client, SEARCH, [system("Be terse."), question])) == "hit"
    assert verdict(await ask_messages(cache_client, SEARCH, [question])) == "miss"  # no system prompt: another scope

    turn1 = [
        {"role": "user", "content": "What is connection pooling?"},
        {"role": "assistant", "content": "It reuses connections."},
    ]
    followup = {"role": "user", "content": "What happens when the pool is exhausted?"}
    assert verdict(await ask_messages(cache_client, SEARCH, [*turn1, followup])) == "miss"
    assert verdict(await ask_messages(cache_client, SEARCH, [*turn1, followup])) == "hit"
    other = [
        {"role": "user", "content": "What is a thread pool?"},
        {"role": "assistant", "content": "A set of workers."},
    ]
    assert (
        verdict(await ask_messages(cache_client, SEARCH, [*other, followup])) == "miss"
    )  # same last message, other conversation


# ---- what is never cached -------------------------------------------------------------------------


async def test_time_sensitive_prompts_are_never_cached(cache_client, fakes):
    for _ in range(2):
        assert verdict(await ask(cache_client, SEARCH, "What is the current status of the payments service?")) == "miss"
    assert len(fakes["alpha"].calls) == 2
    assert {e["cache"] for e in await logs(cache_client, SEARCH)} == {"bypass"}  # never even looked up
    assert (await stats(cache_client, SEARCH))["entries"] == 0


async def test_a_key_with_the_cache_off_never_caches(cache_client, fakes):
    for _ in range(2):
        await ask(cache_client, RESEARCH, "What is a message queue?")
    assert len(fakes["alpha"].calls) == 2
    assert {e["cache"] for e in await logs(cache_client, RESEARCH)} == {"bypass"}
    assert (await stats(cache_client, RESEARCH))["entries"] == 0


async def test_very_long_prompts_bypass_the_cache(cache_client):
    prompt = "Describe the following service in detail: " + "billing " * 700
    assert len(prompt) > 4000
    await ask(cache_client, SEARCH, prompt)
    assert verdict(await ask(cache_client, SEARCH, prompt)) == "miss"
    assert (await logs(cache_client, SEARCH))[0]["cache"] == "bypass"


async def test_failed_upstream_responses_are_never_cached(cache_client, fakes):
    fakes["alpha"].fail_times = fakes["beta"].fail_times = 99
    assert (await ask(cache_client, SEARCH, "What is a message queue?")).status_code == 502
    assert (await stats(cache_client, SEARCH))["entries"] == 0
    assert (await logs(cache_client, SEARCH))[0]["cache"] == "miss"  # the cache was consulted, there was nothing
    fakes["alpha"].fail_times = fakes["beta"].fail_times = 0
    assert verdict(await ask(cache_client, SEARCH, "What is a message queue?")) == "miss"
    assert verdict(await ask(cache_client, SEARCH, "What is a message queue?")) == "hit"


async def test_a_partial_stream_is_never_cached(cache_client, fakes):
    fakes["alpha"].reply = "one two three four five six"
    fakes["alpha"].fail_stream_after = 2
    resp = await cache_client.post(
        "/v1/chat/completions",
        json={"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Explain gateways"}]},
        headers=auth(SEARCH),
    )
    assert "stream_interrupted" in resp.text
    assert (await stats(cache_client, SEARCH))["entries"] == 0
    fakes["alpha"].fail_stream_after = None
    assert verdict(await ask(cache_client, SEARCH, "Explain gateways")) == "miss"


# ---- streaming ------------------------------------------------------------------------------------


async def test_a_completed_stream_is_cached_and_a_later_plain_request_hits_it(cache_client, fakes):
    body = {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Explain gateways"}]}
    streamed = await cache_client.post("/v1/chat/completions", json=body, headers=auth(SEARCH))
    assert verdict(streamed) == "miss" and sse_lines(streamed.text)[-1] == "data: [DONE]"
    hit = await ask(cache_client, SEARCH, "Explain gateways")
    assert verdict(hit) == "hit" and hit.json()["choices"][0]["message"]["content"].strip() == REPLY
    assert len(fakes["alpha"].calls) == 1


async def test_a_cache_hit_is_replayed_to_a_streaming_caller_as_a_proper_sse_stream(cache_client, fakes):
    await ask(cache_client, SEARCH, "Explain gateways")
    body = {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Explain gateways"}]}
    resp = await cache_client.post("/v1/chat/completions", json=body, headers=auth(SEARCH))

    assert "text/event-stream" in resp.headers["content-type"]
    assert (verdict(resp), resp.headers["x-prism-provider"], resp.headers["x-prism-cost-usd"]) == ("hit", "cache", "0")
    lines = sse_lines(resp.text)
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks).strip() == REPLY
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert len(fakes["alpha"].calls) == 1
    assert (await logs(cache_client, SEARCH))[0]["status"] == "cache_hit"


# ---- rate limit and budget still apply ------------------------------------------------------------


async def test_hits_still_count_against_the_rate_limit(cache_client, fakes):
    statuses = [(await ask(cache_client, FREE, "What is a message queue?")).status_code for _ in range(12)]
    assert statuses == [200] * 10 + [429] * 2  # 1 miss + 9 hits, then limited
    assert len(fakes["alpha"].calls) == 1


async def test_the_budget_is_checked_before_the_cache(cache_client, fakes):
    """Policy: a key whose monthly budget is spent is rejected even for a free cached answer."""
    await enable_cache(BUDGET_DEMO)
    fakes["alpha"].reply = " ".join(["word"] * 20)  # 12_750 nano: more than the 10_000 budget
    assert (await ask(cache_client, BUDGET_DEMO, "What is a message queue?")).status_code == 200
    second = await ask(cache_client, BUDGET_DEMO, "What is a message queue?")  # cached, but the budget is gone
    assert second.status_code == 402 and second.json()["error"]["type"] == "budget_exceeded"


# ---- lifecycle ------------------------------------------------------------------------------------


async def test_entries_expire_after_the_ttl_and_are_purged(cache_client):
    now = [2_000_000_000.0]
    app.state.cache._clock = lambda: now[0]
    app.state.cache.ttl_s = 1000
    await ask(cache_client, SEARCH, "What is a message queue?")
    now[0] += 900
    assert verdict(await ask(cache_client, SEARCH, "What is a message queue?")) == "hit"
    now[0] += 200  # 1100 s after it was stored
    assert verdict(await ask(cache_client, SEARCH, "What is a message queue?")) == "miss"
    assert (await stats(cache_client, SEARCH))["entries"] == 1  # the expired entry was purged, not left to pile up


async def test_the_least_recently_used_entries_go_first_over_the_limit(cache_client):
    app.state.cache.max_entries_per_key = 3
    topics = ["billing", "search", "email", "upload", "export"]

    async def prompt(topic):
        await asyncio.sleep(0.005)  # distinct timestamps, so "least recently used" is well defined
        return verdict(await ask(cache_client, SEARCH, f"Describe the {topic} service"))

    for topic in topics[:3]:
        assert await prompt(topic) == "miss"
    assert await prompt("billing") == "hit"  # touch the oldest so it is no longer the least recently used
    assert await prompt("upload") == "miss"  # a 4th entry: one must go
    assert (await stats(cache_client, SEARCH))["entries"] == 3
    assert await prompt("billing") == "hit" and await prompt("email") == "hit"
    assert await prompt("search") == "miss"  # "search" was the least recently used, so it was evicted


async def test_concurrent_identical_requests_leave_a_single_entry(cache_client):
    responses = await asyncio.gather(*(ask(cache_client, SEARCH, "What is a message queue?") for _ in range(10)))
    assert all(r.status_code == 200 for r in responses)
    assert (await stats(cache_client, SEARCH))["entries"] == 1  # twins are merged, not duplicated
    assert verdict(await ask(cache_client, SEARCH, "What is a message queue?")) == "hit"


async def test_the_cache_survives_a_gateway_restart(fakes):
    def install(fake_map):
        registry = ProviderRegistry(dict(fake_map))
        app.state.providers = registry
        app.state.upstream = Upstream(registry, ProviderHealth(), RetryPolicy(initial_backoff_ms=1), sleep=noop_sleep)

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        install(fakes)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            assert verdict(await ask(client, SEARCH, A1)) == "miss"

    fresh = {"alpha": FakeAdapter("alpha"), "beta": FakeAdapter("beta")}
    async with app.router.lifespan_context(app):  # a new process, the same database file
        install(fresh)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            assert verdict(await ask(client, SEARCH, A2)) == "hit"  # a paraphrase, served from the persisted entry
    assert fresh["alpha"].calls == []


# ---- the cache may never break a request ----------------------------------------------------------


async def test_a_broken_cache_lookup_degrades_to_a_miss_not_an_error(cache_client, fakes, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("cache store unavailable")

    monkeypatch.setattr(app.state.cache, "lookup", boom)
    monkeypatch.setattr(app.state.cache, "store_response", boom)
    resp = await ask(cache_client, SEARCH, "What is a message queue?")
    assert resp.status_code == 200 and verdict(resp) == "miss"
    assert len(fakes["alpha"].calls) == 1


# ---- accounting and the admin API -----------------------------------------------------------------


async def test_costs_and_counts_reconcile_across_hits_and_misses(cache_client):
    prompts = [A1, A2, A3, A1, "What is a load balancer?", "What is a load balancer?"]
    responses = [await ask(cache_client, SEARCH, p) for p in prompts]
    assert [verdict(r) for r in responses] == ["miss", "hit", "miss", "hit", "miss", "hit"]
    header_nano = sum(round(float(r.headers["x-prism-cost-usd"]) * 1_000_000_000) for r in responses)
    totals = await usage(cache_client, SEARCH)
    assert (totals["requests"], totals["cache_hits"]) == (6, 3)
    assert totals["cost_nano"] == header_nano == 3 * COST  # only the three misses cost anything


async def test_cache_stats_report_hits_misses_hit_rate_and_money_saved(cache_client):
    for prompt in (A1, A2, A2, A3):  # miss, hit, hit, miss
        await ask(cache_client, SEARCH, prompt)
    await ask(cache_client, RESEARCH, A1)  # cache off: not a lookup at all
    one = await stats(cache_client, SEARCH)
    assert (one["hits"], one["misses"], one["hit_rate"], one["entries"]) == (2, 2, 0.5, 2)
    assert one["saved_nano"] == 2 * COST  # two hits, each avoiding one 3_750-nano call
    assert one["matcher"] == "lexical-content-words-v1"
    everything = await stats(cache_client)
    assert everything["overall"]["hits"] == 2 and {row["key"] for row in everything["by_key"]} == {SEARCH}


async def test_cache_admin_endpoints_need_the_admin_token_and_know_their_keys(cache_client):
    assert (await cache_client.get("/admin/cache/stats")).status_code == 401
    assert (await cache_client.delete("/admin/cache")).status_code == 401
    assert (
        await cache_client.get("/admin/cache/stats", params={"key": SEARCH}, headers=auth(SEARCH))
    ).status_code == 401
    assert (await cache_client.get("/admin/cache/stats", params={"key": "nope"}, headers=ADMIN)).status_code == 404


async def test_the_cache_can_be_purged_overall_or_for_one_key(cache_client):
    await ask(cache_client, SEARCH, A1)
    await ask(cache_client, FREE, A1)
    cleared = await cache_client.delete("/admin/cache", params={"key": SEARCH}, headers=ADMIN)
    assert cleared.json() == {"deleted": 1}
    assert verdict(await ask(cache_client, SEARCH, A1)) == "miss"  # purged
    assert verdict(await ask(cache_client, FREE, A1)) == "hit"  # the other tenant was untouched
    assert (await cache_client.delete("/admin/cache", headers=ADMIN)).json()["deleted"] == 2
    assert verdict(await ask(cache_client, FREE, A1)) == "miss"