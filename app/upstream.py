"""Call the provider chain for one request: retries, failover and health.

Policy (each rule is also pinned by a test):
  * Walk the chain primary -> fallbacks. The first model that answers serves the request.
  * server_error (5xx) and network blips: retry the SAME provider with exponential backoff and
    jitter, up to `max_attempts`; if it keeps failing, move to the next model.
  * connect, timeout, rate_limited (429), malformed response: do NOT retry the same provider
    (retrying a dead or slow endpoint just burns the caller's time); fail over immediately.
  * client_error: HTTP 400/413/422 means our request itself is bad, so another provider would
    fail the same way: stop. Other 4xx (401/403/404) are provider-side problems: fail over.
  * A degraded provider (see health.py) is skipped, unless it is the last model in the chain.
  * One overall deadline bounds retries plus failover, so a caller is never kept waiting long.
  * Streams use the same walk, but only until the FIRST chunk: after that the client has
    output and failover is impossible (that case is handled in streaming.py).
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio

from app.errors import upstream_failed
from app.health import ProviderHealth
from app.providers import (
    ChatResult,
    ProviderError,
    ProviderRegistry,
    StreamChunk,
    UnknownModelError,
)

logger = logging.getLogger("prism.upstream")

RETRY_SAME_PROVIDER = {"server_error", "network"}
CALLER_FAULT_STATUSES = {400, 413, 422}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_ms: int = 200
    backoff_multiplier: float = 2.0
    deadline_s: float = 10.0

    @classmethod
    def from_config(cls, config: dict, *, deadline_s: float) -> "RetryPolicy":
        retry = config.get("retry", {})
        return cls(
            max_attempts=retry.get("max_attempts", 3),
            initial_backoff_ms=retry.get("initial_backoff_ms", 200),
            backoff_multiplier=retry.get("backoff_multiplier", 2),
            deadline_s=deadline_s,
        )

    def backoff_s(self, attempt: int, rng: Callable[[], float]) -> float:
        """Delay after the Nth failed attempt: 200ms, 400ms, ... with +-25% jitter, so many
        clients retrying at once do not all hit the provider at the same instant."""
        base = self.initial_backoff_ms / 1000 * self.backoff_multiplier ** (attempt - 1)
        return base * (0.75 + 0.5 * rng())


@dataclass(frozen=True)
class Outcome:
    result: ChatResult
    provider: str
    model: str
    fallback: bool  # True only when a fallback (not the primary) served the request
    retries: int  # upstream attempts beyond the first, failovers included


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


@dataclass(frozen=True)
class _Served:
    value: Any
    provider: str
    model: str
    fallback: bool
    retries: int


def _next_step(error: ProviderError) -> str:
    """'retry' the same provider, go to the 'next' model, or 'stop' entirely."""
    if error.kind == "client_error":
        return "stop" if error.status in CALLER_FAULT_STATUSES else "next"
    return "retry" if error.kind in RETRY_SAME_PROVIDER else "next"


class Upstream:
    def __init__(
        self,
        registry: ProviderRegistry,
        health: ProviderHealth,
        policy: RetryPolicy,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.registry = registry
        self.health = health
        self.policy = policy
        self._sleep = sleep
        self._rng = rng
        self._clock = clock

    async def chat(self, chain: list[str], messages: list[dict], params: dict) -> Outcome:
        async def call(adapter, model):
            return await adapter.chat(model, messages, **params)

        served = await self._run(chain, call)
        return Outcome(served.value, served.provider, served.model, served.fallback, served.retries)

    async def open_stream(self, chain: list[str], messages: list[dict], params: dict) -> OpenStream:
        async def call(adapter, model):
            stream = adapter.stream(model, messages, **params)
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                raise ProviderError(adapter.name, "network", transient=True, detail="empty stream") from None
            except BaseException:  # includes cancellation: never leave the upstream connection open
                with anyio.CancelScope(shield=True):
                    await stream.aclose()
                raise
            return first, stream

        served = await self._run(chain, call)
        first, stream = served.value
        return OpenStream(first, stream, served.provider, served.model, served.fallback, served.retries)

    async def _run(self, chain: list[str], call) -> _Served:
        started = self._clock()
        attempts = 0
        for index, model in enumerate(chain):
            try:
                adapter = self.registry.provider_for(model)
            except UnknownModelError:
                logger.error("no provider is registered for model %s", model)
                continue
            if index < len(chain) - 1 and self.health.should_skip(adapter.name):
                logger.warning("skipping degraded provider %s, going to the next model", adapter.name)
                continue

            for attempt in range(1, self.policy.max_attempts + 1):
                if self._clock() - started >= self.policy.deadline_s:
                    logger.warning("upstream deadline of %ss reached", self.policy.deadline_s)
                    raise upstream_failed(retries=max(attempts - 1, 0))
                attempts += 1
                try:
                    value = await call(adapter, model)
                except ProviderError as exc:
                    logger.warning("%s detail=%s", exc, exc.detail)  # detail is for logs, never for callers
                    step = _next_step(exc)
                    if step == "stop":
                        raise upstream_failed(retries=attempts - 1) from exc
                    if exc.kind != "client_error":
                        self.health.record(adapter.name, ok=False)
                    if step == "retry" and attempt < self.policy.max_attempts:
                        await self._sleep(self.policy.backoff_s(attempt, self._rng))
                        continue
                    break  # next model in the chain
                self.health.record(adapter.name, ok=True)
                return _Served(value, adapter.name, model, fallback=index > 0, retries=attempts - 1)
        raise upstream_failed(retries=max(attempts - 1, 0))