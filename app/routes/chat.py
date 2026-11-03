"""Data plane: POST /v1/chat/completions (OpenAI-compatible).

Order of checks, documented because the brief asks for it:
  1. authenticate the virtual key          -> 401 authentication_error
  2. validate the body                     -> 400 invalid_request_error
  3. resolve model/alias                   -> 404 not_found_error
  4. model allowlist                       -> 403 model_not_allowed
  5. (Phase 4) rate limit -> 429, then budget -> 429/402
  6. (Phase 7) semantic cache
  7. upstream call with timeout (retries and failover in Phase 5)
Every request is logged, including every rejection.
"""

import logging
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.costs import compute_cost_nano
from app.errors import (
    GatewayError,
    authentication_error,
    error_response,
    invalid_request,
    model_not_allowed,
    not_implemented,
)
from app.headers import prism_headers
from app.routing import resolve_route
from app.store import KeyRecord, LogEntry, Store
from app.upstream import call_upstream

logger = logging.getLogger("prism.chat")
router = APIRouter()

# Only these optional OpenAI parameters are forwarded upstream; everything else is dropped.
PASS_THROUGH = ("temperature", "top_p", "max_tokens", "stop", "seed", "presence_penalty", "frequency_penalty")


async def authenticate(request: Request, store: Store) -> KeyRecord:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 200:
        raise authentication_error()
    key = await store.get_active_key(token)
    if key is None:
        raise authentication_error()
    return key


async def read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise invalid_request("Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise invalid_request("Request body must be a JSON object")
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise invalid_request("'model' is required and must be a string")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid_request("'messages' must be a non-empty list")
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str) or "content" not in message:
            raise invalid_request("each message needs a 'role' and 'content'")
    return body


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    state = request.app.state
    store: Store = state.store
    request_id = "req_" + uuid.uuid4().hex[:16]
    started = time.monotonic()
    created_at = datetime.now(UTC)
    seen = {"virtual_key": None, "requested_model": None}  # what we know if we have to reject

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        key = await authenticate(request, store)
        seen["virtual_key"] = key.virtual_key
        body = await read_body(request)
        requested = body["model"]
        seen["requested_model"] = requested[:100]
        route = resolve_route(state.config, state.pricing, requested, body["messages"])
        if requested not in key.allowlist:
            raise model_not_allowed(requested)
        if body.get("stream"):
            raise not_implemented("Streaming is not available yet")
        params = {k: body[k] for k in PASS_THROUGH if k in body}
        outcome = await call_upstream(state.providers, route.chain, body["messages"], params)
    except GatewayError as err:
        entry = LogEntry(request_id, created_at, err.log_status, latency_ms=elapsed_ms(), **seen)
        try:
            await store.log_request(entry)
        except Exception:  # a logging failure must not hide the real error from the caller
            logger.exception("failed to log rejected request %s", request_id)
        return error_response(err, request_id)

    usage = outcome.result.usage
    cost_nano = compute_cost_nano(state.pricing, outcome.model, usage)
    await store.record_served(
        LogEntry(
            request_id,
            created_at,
            "ok",
            virtual_key=key.virtual_key,
            requested_model=seen["requested_model"],
            resolved_provider=outcome.provider,
            resolved_model=outcome.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_nano=cost_nano,
            cache="miss",
            fallback=outcome.fallback,
            route_reason=route.reason,
            retries=outcome.retries,
            latency_ms=elapsed_ms(),
            upstream_ms=outcome.result.latency_ms,
        )
    )
    headers = prism_headers(
        provider=f"{outcome.provider}/{outcome.model}",
        cache="miss",
        fallback=outcome.fallback,
        cost_nano=cost_nano,
        request_id=request_id,
    )
    return JSONResponse(outcome.result.body, headers=headers)