"""Call the provider chain for one request.

Phase 2: a single attempt on the primary model. Phase 5 turns this into retries with
backoff plus failover down the chain; the signature stays the same.
"""

import logging
from dataclasses import dataclass

from app.errors import upstream_failed
from app.providers import ChatResult, ProviderError, ProviderRegistry, UnknownModelError

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