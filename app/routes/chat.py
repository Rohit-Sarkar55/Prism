"""Data plane: POST /v1/chat/completions (OpenAI-compatible), streaming and non-streaming.

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
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.costs import compute_cost_nano
from app.errors import (
    GatewayError,
    authentication_error,
    error_response,
    invalid_request,
    model_not_allowed,
)
from app.headers import prism_headers
from app.providers import Usage
from app.routing import Route, resolve_route
from app.store import KeyRecord, LogEntry, Store
from app.streaming import sse_stream
from app.upstream import call_upstream, open_stream

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


@dataclass
class RequestContext:
    """What we know about one request, used to build its log row whatever the outcome."""

    request_id: str
    created_at: datetime
    started: float
    virtual_key: str | None = None
    requested_model: str | None = None
    route: Route | None = None

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def rejected(self, err: GatewayError) -> LogEntry:
        return LogEntry(
            self.request_id,
            self.created_at,
            err.log_status,
            virtual_key=self.virtual_key,
            requested_model=self.requested_model,
            latency_ms=self.elapsed_ms(),
        )

    def served(self, status: str, provider: str, model: str, usage: Usage, cost_nano: int, **extra) -> LogEntry:
        return LogEntry(
            self.request_id,
            self.created_at,
            status,
            virtual_key=self.virtual_key,
            requested_model=self.requested_model,
            resolved_provider=provider,
            resolved_model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_nano=cost_nano,
            route_reason=self.route.reason if self.route else None,
            latency_ms=self.elapsed_ms(),
            **extra,
        )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    state = request.app.state
    store: Store = state.store
    ctx = RequestContext(
        request_id="req_" + uuid.uuid4().hex[:16], created_at=datetime.now(UTC), started=time.monotonic()
    )
    streaming = False
    try:
        key = await authenticate(request, store)
        ctx.virtual_key = key.virtual_key
        body = await read_body(request)
        ctx.requested_model = body["model"][:100]
        ctx.route = resolve_route(state.config, state.pricing, body["model"], body["messages"])
        if body["model"] not in key.allowlist:
            raise model_not_allowed(body["model"])
        params = {k: body[k] for k in PASS_THROUGH if k in body}
        streaming = bool(body.get("stream"))
        upstream_started = time.monotonic()
        if streaming:
            opened = await open_stream(state.providers, ctx.route.chain, body["messages"], params)
        else:
            outcome = await call_upstream(state.providers, ctx.route.chain, body["messages"], params)
    except GatewayError as err:
        try:
            await store.log_request(ctx.rejected(err))
        except Exception:  # a logging failure must not hide the real error from the caller
            logger.exception("failed to log rejected request %s", ctx.request_id)
        return error_response(err, ctx.request_id)

    if streaming:
        return _stream_response(state, store, ctx, opened, upstream_started)
    return await _complete(state, store, ctx, outcome)


async def _complete(state, store: Store, ctx: RequestContext, outcome) -> JSONResponse:
    usage = outcome.result.usage
    cost_nano = compute_cost_nano(state.pricing, outcome.model, usage)
    await store.record_served(
        ctx.served(
            "ok",
            outcome.provider,
            outcome.model,
            usage,
            cost_nano,
            fallback=outcome.fallback,
            retries=outcome.retries,
            upstream_ms=outcome.result.latency_ms,
        )
    )
    headers = prism_headers(
        provider=f"{outcome.provider}/{outcome.model}",
        fallback=outcome.fallback,
        cost_nano=cost_nano,
        request_id=ctx.request_id,
    )
    return JSONResponse(outcome.result.body, headers=headers)


def _stream_response(state, store: Store, ctx: RequestContext, opened, upstream_started: float) -> StreamingResponse:
    async def record(status: str, usage: Usage | None) -> None:
        usage = usage or Usage(0, 0)
        cost_nano = compute_cost_nano(state.pricing, opened.model, usage)
        await store.record_served(
            ctx.served(
                status,
                opened.provider,
                opened.model,
                usage,
                cost_nano,
                fallback=opened.fallback,
                retries=opened.retries,
                upstream_ms=int((time.monotonic() - upstream_started) * 1000),
            )
        )

    # Headers are sent before the body, so the final cost is not known yet: x-prism-cost-usd is
    # omitted (the API contract allows this for streams). The cost goes to the log and usage API.
    headers = prism_headers(
        provider=f"{opened.provider}/{opened.model}",
        fallback=opened.fallback,
        cost_nano=None,
        request_id=ctx.request_id,
    )
    headers["cache-control"] = "no-cache"
    headers["x-accel-buffering"] = "no"  # ask reverse proxies (nginx) not to buffer the stream
    return StreamingResponse(sse_stream(opened, record), media_type="text/event-stream", headers=headers)