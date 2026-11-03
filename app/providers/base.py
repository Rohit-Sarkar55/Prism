"""Provider adapter contract: what the gateway core needs from any upstream.

The gateway never talks HTTP to a provider directly. It only sees this interface, so mock
providers, real providers and in-process fakes are interchangeable.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ChatResult:
    body: dict[str, Any]  # OpenAI-shaped completion, as returned upstream
    usage: Usage  # provider-reported; the only source we trust for cost
    provider: str
    model: str
    latency_ms: int


@dataclass(frozen=True)
class StreamChunk:
    data: dict[str, Any]  # one parsed OpenAI chunk object (the gateway re-frames it as SSE)
    usage: Usage | None = None  # set on the chunk that carries final usage


class ProviderError(Exception):
    """Any failure talking to a provider. `str(error)` is always safe to show or log.

    `kind` is one of: connect, network, timeout, rate_limited, server_error, client_error,
    malformed_response, stream_interrupted.
    `transient` means "retrying the same provider may help". Whether to fail over is the
    gateway's decision (Phase 5), not the adapter's.
    `detail` is the upstream's own message (secrets redacted) for logs only. Never send it to
    callers: upstream messages can describe our credentials or infrastructure.
    """

    def __init__(
        self,
        provider: str,
        kind: str,
        *,
        status: int | None = None,
        transient: bool = False,
        retry_after_s: float | None = None,
        detail: str = "",
    ):
        self.provider = provider
        self.kind = kind
        self.status = status
        self.transient = transient
        self.retry_after_s = retry_after_s
        self.detail = detail
        suffix = f" (HTTP {status})" if status else ""
        super().__init__(f"provider '{provider}' failed: {kind}{suffix}")


class MidStreamError(ProviderError):
    """Failure after the first chunk was already yielded. The client has partial output, so the
    gateway must end the stream with an error event and must NOT fail over and splice another
    provider's answer onto it.
    """

    def __init__(self, provider: str, *, detail: str = ""):
        super().__init__(provider, "stream_interrupted", transient=False, detail=detail)


class ProviderAdapter(Protocol):
    name: str

    async def chat(self, model: str, messages: list[dict], **params: Any) -> ChatResult: ...

    def stream(self, model: str, messages: list[dict], **params: Any) -> AsyncIterator[StreamChunk]: ...

    async def aclose(self) -> None: ...