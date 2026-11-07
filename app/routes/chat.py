"""Data plane: POST /v1/chat/completions (OpenAI-compatible), streaming and non-streaming.

Order of checks, documented because the brief asks for it:
  1. authenticate the virtual key          -> 401 authentication_error
  2. validate the body                     -> 400 invalid_request_error
  3. resolve model/alias                   -> 404 not_found_error
  4. model allowlist                       -> 403 model_not_allowed
  5. requests-per-minute limit             -> 429 rate_limit_exceeded (with Retry-After)
  6. monthly budget (spent < budget)       -> 402 budget_exceeded
  7. semantic cache lookup (scoped per key, alias, params and earlier conversation) -> hit: no upstream call
  8. upstream call: timeout per attempt, retries with backoff, failover down the chain (upstream.py)
Every request is logged, including every rejection.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.cache import CacheHit, CachePlan, completion_body
from app.costs import compute_cost_nano
from app.errors import (
    GatewayError,
    authentication_error,
    budget_exhausted,
    error_response,
    invalid_request,
    model_not_allowed,
    rate_limited,
)
from app.headers import prism_headers
from app.providers import Usage
from app.routing import Route, resolve_route
from app.store import KeyRecord, LogEntry, Store
from app.streaming import replay_stream, sse_stream

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
    cache_state: str = "bypass"  # becomes "miss" once the cache was actually consulted

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def rejected(self, err: GatewayError) -> LogEntry:
        return LogEntry(
            self.request_id,
            self.created_at,
            err.log_status,
            virtual_key=self.virtual_key,
            requested_model=self.requested_model,
            cache=self.cache_state,
            retries=err.retries,
            latency_ms=self.elapsed_ms(),
        )

    def served(
        self, status: str, provider: str | None, model: str | None, usage: Usage, cost_nano: int, **extra
    ) -> LogEntry:
        fields = {
            "virtual_key": self.virtual_key,
            "requested_model": self.requested_model,
            "resolved_provider": provider,
            "resolved_model": model,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "cost_nano": cost_nano,
            "cache": self.cache_state,
            "route_reason": self.route.reason if self.route else None,
            "latency_ms": self.elapsed_ms(),
        }
        fields.update(extra)
        return LogEntry(self.request_id, self.created_at, status, **fields)


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
        decision = state.limiter.acquire(key.virtual_key, key.rpm)  # atomic: check and record together
        if not decision.allowed:
            raise rate_limited(key.rpm, decision.retry_after_s)
        spent = await store.month_spent_nano(key.virtual_key, ctx.created_at.strftime("%Y-%m"))
        if spent >= key.monthly_budget_nano:  # admit while budget remains; see "overshoot" in the README
            raise budget_exhausted(key.monthly_budget_nano)
        params = {k: body[k] for k in PASS_THROUGH if k in body}
        streaming = bool(body.get("stream"))
        plan = state.cache.plan(key, body["model"], body["messages"], params)
        if plan is not None:
            ctx.cache_state = "miss"
            hit = await _safe_lookup(state, plan)
            if hit is not None:
                return await _serve_cache_hit(store, ctx, hit, streaming)
        upstream_started = time.monotonic()
        if streaming:
            opened = await state.upstream.open_stream(ctx.route.chain, body["messages"], params)
        else:
            outcome = await state.upstream.chat(ctx.route.chain, body["messages"], params)
    except GatewayError as err:
        try:
            await store.log_request(ctx.rejected(err))
        except Exception:  # a logging failure must not hide the real error from the caller
            logger.exception("failed to log rejected request %s", ctx.request_id)
        return error_response(err, ctx.request_id)

    if streaming:
        return _stream_response(state, store, ctx, opened, upstream_started, plan)
    return await _complete(state, store, ctx, outcome, plan)


async def _complete(state, store: Store, ctx: RequestContext, outcome, plan: CachePlan | None) -> JSONResponse:
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
    if plan is not None:
        choice = (outcome.result.body.get("choices") or [{}])[0]
        await _safe_store(
            state, plan, (choice.get("message") or {}).get("content"), outcome.model,
            choice.get("finish_reason") or "stop", cost_nano,
        )  # fmt: skip
    headers = prism_headers(
        provider=f"{outcome.provider}/{outcome.model}",
        fallback=outcome.fallback,
        cost_nano=cost_nano,
        request_id=ctx.request_id,
    )
    return JSONResponse(outcome.result.body, headers=headers)


def _stream_response(
    state, store: Store, ctx: RequestContext, opened, upstream_started: float, plan: CachePlan | None
) -> StreamingResponse:
    async def record(status: str, usage: Usage | None, content: str) -> None:
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
        if plan is not None and status == "ok":  # a partial stream (provider died, client left) is never cached
            await _safe_store(state, plan, content, opened.model, "stop", cost_nano)

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


# ---- cache helpers: the cache is an optimisation, so it may never break a request -----------------


async def _safe_lookup(state, plan: CachePlan) -> CacheHit | None:
    try:
        return await state.cache.lookup(plan)
    except Exception:
        logger.exception("cache lookup failed; treating as a miss")
        return None


async def _safe_store(state, plan: CachePlan, content, model: str, finish_reason: str, cost_nano: int) -> None:
    if not isinstance(content, str):
        return
    try:
        await state.cache.store_response(
            plan, content=content, model=model, finish_reason=finish_reason, cost_nano=cost_nano
        )
    except Exception:
        logger.exception("cache store failed; the response was still served")


async def _serve_cache_hit(store: Store, ctx: RequestContext, hit: CacheHit, streaming: bool):
    note = f"cache hit (similarity {hit.similarity:.3f}, entry {hit.entry_id})"
    reason = f"{ctx.route.reason} | {note}" if ctx.route and ctx.route.reason else note
    entry = ctx.served(
        "cache_hit", None, None, Usage(0, 0), 0,  # nothing consumed upstream: no provider, no tokens, no cost
        cache="hit", cache_similarity=round(hit.similarity, 4), route_reason=reason,
    )  # fmt: skip
    await store.record_served(entry, cache_entry_id=hit.entry_id)
    headers = prism_headers(provider="cache", cache="hit", fallback=False, cost_nano=0, request_id=ctx.request_id)
    if streaming:
        headers["cache-control"] = "no-cache"
        headers["x-accel-buffering"] = "no"
        return StreamingResponse(replay_stream(hit), media_type="text/event-stream", headers=headers)
    return JSONResponse(completion_body(hit), headers=headers)