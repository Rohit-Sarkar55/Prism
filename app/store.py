"""All reads and writes of keys, usage and request logs live here, behind one small class.

Accounting rule: a served request's log row and its usage increment are written in ONE
transaction, under one lock, so the usage totals can never disagree with the logs.
"""

import asyncio
import json
from dataclasses import dataclass, fields
from datetime import datetime

import aiosqlite

from app.money import nano_to_usd_str

# Statuses that reached a provider (or the cache) and delivered output. Only these count in usage.
SERVED = ("ok", "cache_hit", "stream_error", "client_disconnect")


@dataclass
class KeyRecord:
    virtual_key: str
    team: str
    monthly_budget_nano: int
    rpm: int
    tpm: int | None
    allowlist: list[str]
    cache_enabled: bool
    cache_threshold: float | None


@dataclass
class LogEntry:
    request_id: str
    created_at: datetime
    status: str
    virtual_key: str | None = None
    requested_model: str | None = None
    resolved_provider: str | None = None
    resolved_model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_nano: int = 0
    cache: str = "miss"
    fallback: bool = False
    route_reason: str | None = None
    retries: int = 0
    latency_ms: int | None = None
    upstream_ms: int | None = None

    def row(self) -> tuple:
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        values["created_at"] = self.created_at.isoformat(timespec="milliseconds")  # fixed width: sorts as text
        values["fallback"] = int(self.fallback)
        return tuple(values[name] for name in LOG_FIELDS)


LOG_FIELDS = [f.name for f in fields(LogEntry)]
LOG_COLUMNS = ", ".join(LOG_FIELDS)
LOG_PLACEHOLDERS = ", ".join("?" for _ in LOG_FIELDS)


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self._write_lock = asyncio.Lock()

    # ---- keys ----------------------------------------------------------------------------

    async def get_active_key(self, virtual_key: str) -> KeyRecord | None:
        cur = await self.db.execute(
            "SELECT * FROM virtual_keys WHERE virtual_key = ? AND status = 'active'", (virtual_key,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return KeyRecord(
            virtual_key=row["virtual_key"],
            team=row["team"],
            monthly_budget_nano=row["monthly_budget_nano"],
            rpm=row["rpm"],
            tpm=row["tpm"],
            allowlist=json.loads(row["model_allowlist"]),
            cache_enabled=bool(row["cache_enabled"]),
            cache_threshold=row["cache_threshold"],
        )

    async def key_exists(self, virtual_key: str) -> bool:
        cur = await self.db.execute("SELECT 1 FROM virtual_keys WHERE virtual_key = ?", (virtual_key,))
        return await cur.fetchone() is not None

    # ---- writes --------------------------------------------------------------------------

    async def log_request(self, entry: LogEntry) -> None:
        """Log a request that consumed nothing (rejections, upstream failures)."""
        async with self._write_lock:
            await self.db.execute(f"INSERT INTO request_logs ({LOG_COLUMNS}) VALUES ({LOG_PLACEHOLDERS})", entry.row())
            await self.db.commit()

    async def record_served(self, entry: LogEntry) -> None:
        """Log a served request and add it to the key's monthly usage, atomically."""
        month = entry.created_at.strftime("%Y-%m")
        async with self._write_lock:
            try:
                await self.db.execute(
                    f"INSERT INTO request_logs ({LOG_COLUMNS}) VALUES ({LOG_PLACEHOLDERS})", entry.row()
                )
                await self.db.execute(
                    """
                    INSERT INTO usage_monthly (virtual_key, month, requests, prompt_tokens,
                                               completion_tokens, cost_nano, cache_hits)
                    VALUES (?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(virtual_key, month) DO UPDATE SET
                        requests = requests + 1,
                        prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                        completion_tokens = completion_tokens + excluded.completion_tokens,
                        cost_nano = cost_nano + excluded.cost_nano,
                        cache_hits = cache_hits + excluded.cache_hits
                    """,
                    (
                        entry.virtual_key,
                        month,
                        entry.prompt_tokens,
                        entry.completion_tokens,
                        entry.cost_nano,
                        int(entry.status == "cache_hit"),
                    ),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    # ---- reads for the admin API ---------------------------------------------------------

    async def usage_summary(self, virtual_key: str, start: datetime, end: datetime) -> dict:
        """Totals over [start, end) from the request log (the admin path, not the hot path)."""
        cur = await self.db.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cost_nano), 0) AS cost_nano,
                   COALESCE(SUM(status = 'cache_hit'), 0) AS cache_hits
            FROM request_logs
            WHERE virtual_key = ? AND status IN (?, ?, ?, ?) AND created_at >= ? AND created_at < ?
            """,
            (virtual_key, *SERVED, _iso(start), _iso(end)),
        )
        return dict(await cur.fetchone())

    async def month_spent_nano(self, virtual_key: str, month: str) -> int:
        """The budget check: one primary-key read. A new month has no row yet, so it reads as 0
        (the budget resets by itself on the first request of the month)."""
        cur = await self.db.execute(
            "SELECT cost_nano FROM usage_monthly WHERE virtual_key = ? AND month = ?", (virtual_key, month)
        )
        row = await cur.fetchone()
        return row["cost_nano"] if row else 0

    async def month_usage(self, virtual_key: str, month: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM usage_monthly WHERE virtual_key = ? AND month = ?", (virtual_key, month)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def recent_logs(self, virtual_key: str | None, limit: int) -> list[dict]:
        sql = "SELECT * FROM request_logs"
        params: list = []
        if virtual_key:
            sql += " WHERE virtual_key = ?"
            params.append(virtual_key)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        out = []
        for row in await cur.fetchall():
            item = dict(row)
            item["fallback"] = bool(item["fallback"])
            item["cost_usd"] = nano_to_usd_str(item["cost_nano"])
            out.append(item)
        return out


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")