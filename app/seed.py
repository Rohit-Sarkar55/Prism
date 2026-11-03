"""Load the provided data files: price table, tenants, and gateway config."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite

from app.money import usd_to_nano


def _read_json(path: Path) -> dict:
    # parse_float=Decimal keeps prices and budgets exact (no binary float surprises).
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def _without_comments(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def load_pricing(path: Path) -> dict[str, dict[str, Decimal]]:
    """model name -> {'input_per_1m': Decimal, 'output_per_1m': Decimal} (USD per 1M tokens)."""
    pricing = _without_comments(_read_json(path))
    for model, price in pricing.items():
        for field in ("input_per_1m", "output_per_1m"):
            if field not in price:
                raise ValueError(f"price table: model '{model}' is missing '{field}'")
            price[field] = Decimal(price[field])
    return pricing


def load_gateway_config(path: Path) -> dict:
    return _read_json(path)


def validate_config(pricing: dict, config: dict) -> None:
    """Fail at startup, not at request time, if aliases point at models we cannot price."""
    for alias, spec in config["model_aliases"].items():
        if "primary" not in spec:
            continue  # 'auto' routes by difficulty to other aliases
        for model in [spec["primary"], *spec.get("fallbacks", [])]:
            if model not in pricing:
                raise ValueError(f"alias '{alias}' uses model '{model}' that has no price")
    known = set(config["model_aliases"])
    for tier, target in config["model_aliases"].get("auto", {}).get("route_by_difficulty", {}).items():
        if target not in known:
            raise ValueError(f"auto tier '{tier}' routes to unknown alias '{target}'")


async def seed_keys(db: aiosqlite.Connection, path: Path) -> int:
    """Insert or update tenants from seed_keys.json. Safe to run on every startup:
    configuration is refreshed, but usage, status and created_at are never touched.
    """
    tenants = _read_json(path)["tenants"]
    now = datetime.now(UTC).isoformat()
    for t in tenants:
        cache = t.get("semantic_cache", {})
        enabled = bool(cache.get("enabled", False))
        threshold = cache.get("similarity_threshold") if enabled else None
        await db.execute(
            """
            INSERT INTO virtual_keys (virtual_key, team, monthly_budget_nano, rpm, tpm,
                                      model_allowlist, cache_enabled, cache_threshold, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(virtual_key) DO UPDATE SET
                team = excluded.team,
                monthly_budget_nano = excluded.monthly_budget_nano,
                rpm = excluded.rpm,
                tpm = excluded.tpm,
                model_allowlist = excluded.model_allowlist,
                cache_enabled = excluded.cache_enabled,
                cache_threshold = excluded.cache_threshold
            """,
            (
                t["virtual_key"],
                t["team"],
                usd_to_nano(t["monthly_budget_usd"]),
                t["rate_limit"]["requests_per_minute"],
                t["rate_limit"].get("tokens_per_minute"),
                json.dumps(t["model_allowlist"]),
                int(enabled),
                float(threshold) if threshold is not None else None,
                now,
            ),
        )
    await db.commit()
    return len(tenants)