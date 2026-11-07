"""Server-sent events pass-through.

Chunks are forwarded the moment they arrive; nothing is collected first. Whatever happens
(normal end, provider dies mid-stream, client disconnects) the request is recorded exactly
once, in a `finally`, from the usage the provider reported on the final chunk.
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio

from app.cache import CacheHit
from app.providers import ProviderError, Usage
from app.upstream import OpenStream

logger = logging.getLogger("prism.stream")

# record(status, usage, content): status is "ok", "stream_error" or "client_disconnect"; content is the
# text assembled from the deltas (only trusted, and only cached, when status is "ok")
Recorder = Callable[[str, Usage | None, str], Awaitable[None]]

MID_STREAM_ERROR = {
    "error": {
        "message": "The upstream provider failed while streaming",
        "type": "upstream_error",
        "code": "stream_interrupted",
    }
}


def _event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _delta_text(data: dict) -> str:
    try:
        text = data["choices"][0]["delta"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return text if isinstance(text, str) else ""


async def sse_stream(opened: OpenStream, record: Recorder) -> AsyncIterator[str]:
    usage: Usage | None = None
    parts: list[str] = []
    status = "client_disconnect"  # stays this way unless we reach the end or the provider fails
    try:
        if opened.first.usage:
            usage = opened.first.usage
        parts.append(_delta_text(opened.first.data))
        yield _event(opened.first.data)
        async for chunk in opened.rest:
            if chunk.usage:
                usage = chunk.usage
            parts.append(_delta_text(chunk.data))
            yield _event(chunk.data)
        status = "ok"  # upstream finished cleanly; set before the last yield in case the client leaves now
        yield "data: [DONE]\n\n"
    except ProviderError as exc:
        # The client already has partial output, so we cannot fail over or splice another
        # provider's answer. End with an error event and NO [DONE]: [DONE] means success.
        status = "stream_error"
        logger.warning("%s detail=%s", exc, exc.detail)
        yield _event(MID_STREAM_ERROR)
    finally:
        # On a client disconnect the surrounding task is cancelled, and a cancelled anyio scope
        # makes every further await raise. Shield the cleanup so the accounting still happens.
        with anyio.CancelScope(shield=True):
            try:
                await opened.aclose()
                if status == "ok" and usage is None:
                    logger.warning("stream finished without provider usage; recording zero cost")
                await record(status, usage, "".join(parts))
            except Exception:
                logger.exception("failed to record streamed request")


async def replay_stream(hit: CacheHit) -> AsyncIterator[str]:
    """Serve a cache hit to a streaming caller as a proper SSE stream (role chunk, content pieces,
    final chunk, [DONE]). It arrives at once: there is no provider to wait for."""
    base = {
        "id": f"chatcmpl-cache-{hit.entry_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": hit.model,
    }

    def chunk(delta: dict, finish: str | None = None, **extra) -> str:
        return _event({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})

    yield chunk({"role": "assistant", "content": ""})
    for piece in re.findall(r"\S+\s*|\s+", hit.content):
        yield chunk({"content": piece})
    yield chunk({}, hit.finish_reason, usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    yield "data: [DONE]\n\n"