"""Streaming: wire format, accounting, failure modes, and the no-buffering guarantee."""

import asyncio
import json

import anyio
import anyio.lowlevel
import pytest

from app.health import ProviderHealth
from app.providers import ProviderRegistry, Usage
from app.providers.fake import FakeAdapter
from app.streaming import sse_stream
from app.upstream import RetryPolicy, Upstream

SEARCH = "prism-sk-search-1a2b3c"
ADMIN = {"Authorization": "Bearer demo-admin-token"}
STREAM_BODY = {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Explain SSE briefly."}]}
REPLY = "one two three four five"  # 5 words -> 5 content chunks + 1 final chunk
COST_5_5 = 3_750  # 5 prompt tokens * 150 + 5 completion tokens * 600 nano-USD on alpha-small


def auth(key: str = SEARCH) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def fakes():
    return {"alpha": FakeAdapter("alpha", reply=REPLY), "beta": FakeAdapter("beta", reply=REPLY)}


def data_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("data: ")]


async def logs(client):
    return (await client.get("/admin/logs", params={"key": SEARCH}, headers=ADMIN)).json()["logs"]


async def usage(client):
    return (await client.get("/admin/usage", params={"key": SEARCH}, headers=ADMIN)).json()


# ---- end to end through the app --------------------------------------------------------


async def test_stream_has_sse_format_headers_and_ends_with_done(client):
    resp = await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth())
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert resp.headers["x-prism-provider"] == "alpha/alpha-small"
    assert resp.headers["x-prism-cache"] == "miss" and resp.headers["x-prism-fallback"] == "false"
    assert "x-prism-cost-usd" not in resp.headers  # not known yet when headers go out; allowed for streams

    lines = data_lines(resp.text)
    assert len(lines) == 7  # 5 content chunks, 1 final chunk, [DONE]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks).split() == REPLY.split()


async def test_completed_stream_records_usage_and_cost_from_the_final_chunk(client):
    await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth())
    entry = (await logs(client))[0]
    assert entry["status"] == "ok" and entry["resolved_model"] == "alpha-small"
    assert (entry["prompt_tokens"], entry["completion_tokens"], entry["cost_nano"]) == (5, 5, COST_5_5)
    assert entry["upstream_ms"] is not None
    totals = await usage(client)
    assert (totals["requests"], totals["cost_nano"]) == (1, COST_5_5)


async def test_streamed_and_non_streamed_requests_cost_the_same(client):
    plain = {**STREAM_BODY, "stream": False}
    resp = await client.post("/v1/chat/completions", json=plain, headers=auth())
    await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth())
    assert resp.headers["x-prism-cost-usd"] == "0.000003750"
    assert [e["cost_nano"] for e in await logs(client)] == [COST_5_5, COST_5_5]


async def test_failure_before_the_first_chunk_is_a_clean_502_not_a_stream(client, fakes):
    fakes["alpha"].fail_times = fakes["beta"].fail_times = 99  # every provider in the chain is down
    resp = await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth())
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    assert "text/event-stream" not in resp.headers["content-type"]
    assert (await logs(client))[0]["status"] == "upstream_error"
    assert (await usage(client))["requests"] == 0


async def test_provider_dying_mid_stream_ends_with_an_error_event_and_no_done(client, fakes):
    fakes["alpha"].fail_stream_after = 2
    resp = await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth())
    assert resp.status_code == 200  # the status line was already sent
    lines = data_lines(resp.text)
    assert "data: [DONE]" not in lines  # [DONE] would tell the client it succeeded
    assert json.loads(lines[-1][6:])["error"]["code"] == "stream_interrupted"
    assert len(lines) == 3  # two real chunks, then the error event
    assert "alpha" not in lines[-1]  # no provider details to the caller
    entry = (await logs(client))[0]
    assert entry["status"] == "stream_error" and entry["cost_nano"] == 0
    assert (await usage(client))["requests"] == 1  # it reached the provider and delivered output


async def test_stream_without_provider_usage_is_recorded_at_zero_cost(client, fakes):
    fakes["alpha"].stream_usage = False
    resp = await client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth())
    assert data_lines(resp.text)[-1] == "data: [DONE]"
    entry = (await logs(client))[0]
    assert (entry["status"], entry["cost_nano"], entry["prompt_tokens"]) == ("ok", 0, 0)


async def test_stream_still_enforces_the_allowlist(client):
    resp = await client.post("/v1/chat/completions", json={**STREAM_BODY, "model": "smart"}, headers=auth())
    assert resp.status_code == 403


async def test_concurrent_streams_are_all_recorded(client):
    n = 15
    responses = await asyncio.gather(
        *(client.post("/v1/chat/completions", json=STREAM_BODY, headers=auth()) for _ in range(n))
    )
    assert all(data_lines(r.text)[-1] == "data: [DONE]" for r in responses)
    totals = await usage(client)
    assert (totals["requests"], totals["cost_nano"]) == (n, n * COST_5_5)


# ---- the generator itself: no buffering, disconnects, shielding -------------------------


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, Usage | None]] = []

    async def __call__(self, status, usage):
        await anyio.lowlevel.checkpoint()  # a real await, so an unshielded cancelled scope would break it
        self.calls.append((status, usage))


async def opened_stream(fake):
    upstream = Upstream(ProviderRegistry({"alpha": fake}), ProviderHealth(), RetryPolicy())
    return await upstream.open_stream(["alpha-small"], [{"role": "user", "content": "hi"}], {})


async def test_first_event_is_forwarded_before_the_upstream_finishes():
    fake = FakeAdapter("alpha", reply=REPLY, chunk_delay_s=0.05)
    events = sse_stream(await opened_stream(fake), Recorder())
    first = await anext(events)
    assert first.startswith("data: ")
    assert fake.emitted < 6  # the provider has not finished: we forwarded without collecting
    await events.aclose()


async def test_normal_end_records_ok_with_usage():
    fake, record = FakeAdapter("alpha", reply=REPLY), Recorder()
    events = [e async for e in sse_stream(await opened_stream(fake), record)]
    assert events[-1] == "data: [DONE]\n\n"
    assert record.calls == [("ok", Usage(5, 5))]
    assert fake.stream_closed


async def test_client_disconnect_is_recorded_and_closes_the_upstream():
    fake, record = FakeAdapter("alpha", reply=REPLY), Recorder()
    events = sse_stream(await opened_stream(fake), record)
    await anext(events)
    await events.aclose()  # what happens when the client goes away
    assert record.calls == [("client_disconnect", None)]
    assert fake.stream_closed  # we stopped reading the provider


async def test_accounting_survives_cancellation_of_the_request_task():
    fake, record = FakeAdapter("alpha", reply=REPLY, chunk_delay_s=0.05), Recorder()
    events = sse_stream(await opened_stream(fake), record)

    async def consume():
        async for _ in events:
            pass

    async with anyio.create_task_group() as tg:  # the same machinery Starlette cancels on disconnect
        tg.start_soon(consume)
        await anyio.sleep(0.12)
        tg.cancel_scope.cancel()

    assert [status for status, _ in record.calls] == ["client_disconnect"]