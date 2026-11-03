"""Call the provider chain for one request.

Phase 2: a single attempt on the primary model. Phase 5 turns this into retries with
backoff plus failover down the chain; the signature stays the same.
"""

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from app.errors import upstream_failed
from app.providers import (
    ChatResult,
    ProviderError,
    ProviderRegistry,
    StreamChunk,
    UnknownModelError,
)

logger = logging.getLogger("prism.upstream")


@dataclass(frozen=True)
class Outcome:
    result: ChatResult
    provider: str
    model: str
    fallback: bool  # True only when a fallback (not the primary) served the request
    retries: int


async def call_upstream(registry: ProviderRegistry, chain: list[str], messages: list[dict], params: dict) -> Outcome:
    model = chain[0]
    try:
        adapter = registry.provider_for(model)
        result = await adapter.chat(model, messages, **params)
    except UnknownModelError as exc:
        logger.error("no provider is registered for model %s", model)
        raise upstream_failed() from exc
    except ProviderError as exc:
        logger.warning("%s detail=%s", exc, exc.detail)  # detail is for logs only, never for callers
        raise upstream_failed() from exc
    return Outcome(result, adapter.name, model, fallback=False, retries=0)


@dataclass
class OpenStream:
    """A stream whose first chunk has already arrived, so we know it is really flowing."""

    first: StreamChunk
    rest: AsyncGenerator[StreamChunk, None]
    provider: str
    model: str
    fallback: bool
    retries: int

    async def aclose(self) -> None:
        await self.rest.aclose()


async def open_stream(registry: ProviderRegistry, chain: list[str], messages: list[dict], params: dict) -> OpenStream:
    """Connect and wait for the first chunk BEFORE any HTTP response is sent. Failures up to
    this point can still become a clean 502 (and, in Phase 5, a failover to the next provider).
    After the first chunk the client has output, so there is no going back.
    """
    model = chain[0]
    try:
        adapter = registry.provider_for(model)
        stream = adapter.stream(model, messages, **params)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            await stream.aclose()
            logger.warning("provider %s returned an empty stream", adapter.name)
            raise upstream_failed() from None
    except UnknownModelError as exc:
        logger.error("no provider is registered for model %s", model)
        raise upstream_failed() from exc
    except ProviderError as exc:
        logger.warning("%s detail=%s", exc, exc.detail)
        raise upstream_failed() from exc
    return OpenStream(first, stream, adapter.name, model, fallback=False, retries=0)