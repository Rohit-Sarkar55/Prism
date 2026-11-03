"""In-process fake provider for unit tests: no network, scriptable failures and delays."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.providers.base import (
    ChatResult,
    MidStreamError,
    ProviderError,
    StreamChunk,
    Usage,
)


class FakeAdapter:
    def __init__(
        self,
        name: str = "fake",
        *,
        reply: str = "hello from the fake provider",
        prompt_tokens: int = 5,
        fail_times: int = 0,
        error: ProviderError | None = None,
        delay_s: float = 0.0,
        fail_stream_after: int | None = None,
        chunk_delay_s: float = 0.0,
        stream_usage: bool = True,
    ):
        self.name = name
        self.reply = reply
        self.prompt_tokens = prompt_tokens
        self.fail_times = fail_times  # fail this many calls, then succeed
        self.error = error
        self.delay_s = delay_s
        self.fail_stream_after = fail_stream_after  # raise MidStreamError after N chunks
        self.chunk_delay_s = chunk_delay_s  # pause before each streamed chunk
        self.stream_usage = stream_usage  # False: the final chunk carries no usage
        self.calls: list[tuple[str, str]] = []  # (kind, model), for assertions
        self.emitted = 0  # chunks handed to the consumer so far
        self.stream_closed = False  # True once a stream generator has been closed or finished

    @property
    def usage(self) -> Usage:
        return Usage(self.prompt_tokens, len(self.reply.split()))

    async def _before(self, kind: str, model: str) -> None:
        self.calls.append((kind, model))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error or ProviderError(self.name, "server_error", status=503, transient=True)

    async def chat(self, model: str, messages: list[dict], **params: Any) -> ChatResult:
        await self._before("chat", model)
        body = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
        }  # fmt: skip
        return ChatResult(body, self.usage, self.name, model, latency_ms=0)

    async def stream(self, model: str, messages: list[dict], **params: Any) -> AsyncIterator[StreamChunk]:
        await self._before("stream", model)
        try:
            for i, word in enumerate(self.reply.split()):
                if self.fail_stream_after is not None and i >= self.fail_stream_after:
                    raise MidStreamError(self.name, detail="fake mid-stream failure")
                if self.chunk_delay_s:
                    await asyncio.sleep(self.chunk_delay_s)
                self.emitted += 1
                yield StreamChunk({"choices": [{"index": 0, "delta": {"content": word + " "}}]})
            self.emitted += 1
            final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield StreamChunk(final, self.usage if self.stream_usage else None)
        finally:
            self.stream_closed = True

    async def aclose(self) -> None:
        return None