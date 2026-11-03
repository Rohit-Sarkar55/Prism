"""Server-sent events pass-through.

Chunks are forwarded the moment they arrive; nothing is collected first. Whatever happens
(normal end, provider dies mid-stream, client disconnects) the request is recorded exactly
once, in a `finally`, from the usage the provider reported on the final chunk.
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio

from app.providers import ProviderError, Usage
from app.upstream import OpenStream

logger = logging.getLogger("prism.stream")

# record(status, usage): status is "ok", "stream_error" or "client_disconnect"
Recorder = Callable[[str, Usage | None], Awaitable[None]]

MID_STREAM_ERROR = {
    "error": {
        "message": "The upstream provider failed while streaming",
        "type": "upstream_error",
        "code": "stream_interrupted",
    }
}


def _event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def sse_stream(opened: OpenStream, record: Recorder) -> AsyncIterator[str]:
    usage: Usage | None = None
    status = "client_disconnect"  # stays this way unless we reach the end or the provider fails
    try:
        if opened.first.usage:
            usage = opened.first.usage
        yield _event(opened.first.data)
        async for chunk in opened.rest:
            if chunk.usage:
                usage = chunk.usage
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
                await record(status, usage)
            except Exception:
                logger.exception("failed to record streamed request")