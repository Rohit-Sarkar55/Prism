"""Adapter behaviour against a scripted transport: no network, fully deterministic."""

import json

import httpx
import pytest

from app.providers import MidStreamError, ProviderError
from app.providers.openai_compat import OpenAICompatAdapter

KEY = "sk-secret-123"
MESSAGES = [{"role": "user", "content": "hi"}]
OK_BODY = {
    "id": "chatcmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
}


def make_adapter(handler):
    return OpenAICompatAdapter(
        "alpha", "http://upstream.test/v1", KEY, timeout_s=1.0, transport=httpx.MockTransport(handler)
    )


def sse(*objs, done=True) -> bytes:
    lines = [f"data: {json.dumps(o)}\n\n" for o in objs]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def sse_response(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})


async def collect(adapter, **kw):
    return [c async for c in adapter.stream("alpha-small", MESSAGES, **kw)]


# ---- non-streaming ---------------------------------------------------------------------


async def test_chat_success_sends_auth_and_returns_provider_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    result = await make_adapter(handler).chat("alpha-small", MESSAGES)

    assert seen["url"] == "http://upstream.test/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["payload"]["model"] == "alpha-small" and seen["payload"]["stream"] is False
    assert (result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens) == (5, 7, 12)
    assert result.provider == "alpha" and result.model == "alpha-small"
    assert result.body["choices"][0]["message"]["content"] == "hello"


@pytest.mark.parametrize(
    ("status", "kind", "transient"),
    [(429, "rate_limited", True), (500, "server_error", True), (503, "server_error", True),
     (404, "client_error", False), (400, "client_error", False), (401, "client_error", False)],
)  # fmt: skip
async def test_http_errors_are_classified(status, kind, transient):
    adapter = make_adapter(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.transient, exc.value.status) == (kind, transient, status)


async def test_rate_limit_carries_retry_after():
    adapter = make_adapter(lambda r: httpx.Response(429, headers={"Retry-After": "5"}, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert exc.value.retry_after_s == 5.0


async def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as exc:
        await make_adapter(handler).chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.transient) == ("timeout", True)


async def test_connection_refused_is_transient():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as exc:
        await make_adapter(handler).chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.transient) == ("connect", True)


async def test_connect_timeout_counts_as_connect_error_not_timeout():
    # What Windows reports for a dead provider: the packets are dropped, nothing refuses.
    def handler(request):
        raise httpx.ConnectTimeout("no answer", request=request)

    with pytest.raises(ProviderError) as exc:
        await make_adapter(handler).chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.transient) == ("connect", True)


async def test_non_json_body_is_malformed():
    adapter = make_adapter(lambda r: httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert exc.value.kind == "malformed_response"


async def test_missing_usage_is_malformed_because_cost_needs_it():
    body = {k: v for k, v in OK_BODY.items() if k != "usage"}
    adapter = make_adapter(lambda r: httpx.Response(200, json=body))
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert exc.value.kind == "malformed_response"


async def test_api_key_never_appears_in_errors():
    leaky = {"error": {"message": f"Invalid key {KEY}"}}
    adapter = make_adapter(lambda r: httpx.Response(401, json=leaky))
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert KEY not in str(exc.value)
    assert KEY not in exc.value.detail and "***" in exc.value.detail


# ---- streaming -------------------------------------------------------------------------


async def test_stream_yields_chunks_and_final_usage_without_done_marker():
    seen = {}
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"content": "hi "}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return sse_response(sse(*chunks))

    out = await collect(make_adapter(handler))

    assert len(out) == 3  # [DONE] is consumed, not yielded: the gateway ends the stream itself
    assert [c.usage for c in out[:2]] == [None, None]
    assert (out[2].usage.prompt_tokens, out[2].usage.completion_tokens) == (3, 2)
    assert seen["payload"]["stream"] is True
    assert seen["payload"]["stream_options"] == {"include_usage": True}


async def test_stream_error_before_first_chunk_is_a_plain_provider_error():
    adapter = make_adapter(lambda r: httpx.Response(503, json={"error": {"message": "down"}}))
    with pytest.raises(ProviderError) as exc:
        await collect(adapter)
    assert not isinstance(exc.value, MidStreamError)
    assert (exc.value.kind, exc.value.transient) == ("server_error", True)


async def test_stream_cut_off_after_first_chunk_is_mid_stream_error():
    body = sse({"choices": [{"delta": {"content": "hi "}}]}, done=False)
    adapter = make_adapter(lambda r: sse_response(body))
    got = []
    with pytest.raises(MidStreamError) as exc:
        async for chunk in adapter.stream("alpha-small", MESSAGES):
            got.append(chunk)
    assert len(got) == 1  # the client already received a chunk, so no failover is possible
    assert exc.value.transient is False


async def test_stream_garbage_after_first_chunk_is_mid_stream_error():
    body = sse({"choices": [{"delta": {"content": "hi "}}]}, done=False) + b"data: {not json}\n\n"
    adapter = make_adapter(lambda r: sse_response(body))
    with pytest.raises(MidStreamError):
        await collect(adapter)


async def test_stream_that_ends_empty_is_a_retryable_network_error():
    adapter = make_adapter(lambda r: sse_response(b""))
    with pytest.raises(ProviderError) as exc:
        await collect(adapter)
    assert not isinstance(exc.value, MidStreamError)
    assert (exc.value.kind, exc.value.transient) == ("network", True)