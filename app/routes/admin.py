"""Admin plane: protected by a simple demo token (Authorization: Bearer <admin token>)."""

import secrets
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Query, Request

from app.errors import GatewayError, authentication_error, invalid_request, not_found
from app.money import nano_to_usd_str

router = APIRouter(prefix="/admin")


def require_admin(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = request.app.state.settings.admin_token
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
        raise authentication_error()


def _parse_day(value: str | None, default: date, name: str) -> date:
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise invalid_request(f"'{name}' must be a date like 2026-11-30") from exc


@router.get("/usage")
async def usage(request: Request, key: str, from_: str | None = Query(None, alias="from"), to: str | None = None):
    require_admin(request)
    store = request.app.state.store
    if not await store.key_exists(key):
        raise not_found(key)
    today = datetime.now(UTC).date()
    start_day = _parse_day(from_, today.replace(day=1), "from")
    end_day = _parse_day(to, today, "to")
    if start_day > end_day:
        raise GatewayError(400, "invalid_request_error", "'from' must not be after 'to'", "rejected_invalid")
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=UTC) + timedelta(days=1)  # 'to' is inclusive
    totals = await store.usage_summary(key, start, end)
    return {
        "key": key,
        "from": start_day.isoformat(),
        "to": end_day.isoformat(),
        "requests": totals["requests"],
        "prompt_tokens": totals["prompt_tokens"],
        "completion_tokens": totals["completion_tokens"],
        "cost_usd": float(nano_to_usd_str(totals["cost_nano"])),
        "cost_nano": totals["cost_nano"],  # exact integer; reconcile against this
        "cache_hits": totals["cache_hits"],
    }


@router.get("/logs")
async def logs(request: Request, key: str | None = None, limit: int = Query(50, ge=1, le=500)):
    require_admin(request)
    return {"logs": await request.app.state.store.recent_logs(key, limit)}