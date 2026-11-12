"""Admin plane: protected by a simple demo token (Authorization: Bearer <admin token>)."""

import json
import secrets
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Query, Request

from app.errors import GatewayError, authentication_error, invalid_request, not_found
from app.money import nano_to_usd_str

router = APIRouter(prefix="/admin")


def require_admin(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = request.app.state.settings.admin_token
    # Compare BYTES: compare_digest on two str values raises TypeError for any non-ASCII character,
    # which would turn a garbage token into a 500 instead of a 401.
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip().encode(), expected.encode()):
        raise authentication_error()


def _parse_day(value: str | None, default: date, name: str) -> date:
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise invalid_request(f"'{name}' must be a date like 2026-11-30") from exc


@router.get("/keys")
async def keys(request: Request):
    """All keys with their limits and this month's spend: the console's usage-by-key table."""
    require_admin(request)
    month = datetime.now(UTC).strftime("%Y-%m")
    out = []
    for row in await request.app.state.store.keys_overview(month):
        budget = row["monthly_budget_nano"]
        out.append(
            {
                "key": row["virtual_key"],
                "team": row["team"],
                "status": row["status"],
                "monthly_budget_usd": float(nano_to_usd_str(budget)),
                "spent_usd": float(nano_to_usd_str(row["cost_nano"])),
                "spent_nano": row["cost_nano"],
                "budget_used_pct": round(row["cost_nano"] / budget * 100, 1) if budget else 0.0,
                "requests": row["requests"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "cache_hits": row["cache_hits"],
                "rpm": row["rpm"],
                "model_allowlist": json.loads(row["model_allowlist"]),
                "cache_enabled": bool(row["cache_enabled"]),
                "cache_threshold": row["cache_threshold"],
            }
        )
    return {"month": month, "keys": out}


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


@router.get("/providers")
async def providers(request: Request):
    require_admin(request)
    health = request.app.state.health.snapshot()
    names = request.app.state.providers.adapters
    return {
        "providers": {
            name: health.get(name, {"state": "healthy", "recent_calls": 0, "recent_error_rate": 0.0}) for name in names
        }
    }


def _cache_row(row: dict) -> dict:
    lookups = row["hits"] + row["misses"]
    return {
        "hits": row["hits"],
        "misses": row["misses"],
        "hit_rate": round(row["hits"] / lookups, 4) if lookups else 0.0,
        "entries": row["entries"],
        "saved_nano": row["saved_nano"],  # exact: what the hits would have cost upstream
        "saved_usd": float(nano_to_usd_str(row["saved_nano"])),
    }


@router.get("/cache/stats")
async def cache_stats(request: Request, key: str | None = None):
    """Hit rate counts only requests that consulted the cache (hit or miss); requests for keys with the
    cache off, uncacheable prompts and rejected requests are not lookups."""
    require_admin(request)
    state = request.app.state
    if key is not None and not await state.store.key_exists(key):
        raise not_found(key)
    rows = await state.store.cache_stats(key)
    matcher = state.cache.embedder.name
    if key is not None:
        row = next(iter(rows), {"hits": 0, "misses": 0, "entries": 0, "saved_nano": 0})
        return {"key": key, "matcher": matcher, **_cache_row(row)}
    total = {f: sum(r[f] for r in rows) for f in ("hits", "misses", "entries", "saved_nano")}
    return {
        "matcher": matcher,
        "overall": _cache_row(total),
        "by_key": [{"key": r["virtual_key"], **_cache_row(r)} for r in rows],
    }


@router.delete("/cache")
async def clear_cache(request: Request, key: str | None = None):
    """Purge cached entries (all, or one key's). Useful before a demo and after changing prompts."""
    require_admin(request)
    return {"deleted": await request.app.state.cache.clear(key)}