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
    cache: str = "bypass"  # hit | miss | bypass
    fallback: bool = False
    route_reason: str | None = None
    retries: int = 0
    latency_ms: int | None = None
    upstream_ms: int | None = None
    cache_similarity: float | None = None

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

    async def record_served(self, entry: LogEntry, *, cache_entry_id: int | None = None) -> None:
        """Log a served request and add it to the key's monthly usage, atomically. For a cache hit,
        the entry's hit counter moves in the same transaction."""
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
                if cache_entry_id is not None:
                    await self.db.execute(
                        "UPDATE cache_entries SET hit_count = hit_count + 1, last_used_at = ? WHERE id = ?",
                        (entry.created_at.isoformat(timespec="milliseconds"), cache_entry_id),
                    )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    # ---- cache entries (the matching logic lives in app/cache.py) -------------------------

    async def load_cache_scope(self, virtual_key: str, model: str, scope_hash: str) -> list[dict]:
        cur = await self.db.execute(
            "SELECT id, embedding, created_at FROM cache_entries WHERE virtual_key = ? AND model = ? AND scope_hash = ?",
            (virtual_key, model, scope_hash),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def get_cache_entry(self, entry_id: int) -> dict | None:
        cur = await self.db.execute("SELECT * FROM cache_entries WHERE id = ?", (entry_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def insert_cache_entry(
        self, virtual_key: str, model: str, scope_hash: str, prompt_text: str, embedding: str,
        response_json: str, cost_nano: int, created_at: datetime,
    ) -> int:  # fmt: skip
        stamp = _iso(created_at)
        async with self._write_lock:
            cur = await self.db.execute(
                """INSERT INTO cache_entries (virtual_key, model, scope_hash, prompt_text, embedding, response_json,
                                              cost_nano, created_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    virtual_key,
                    model,
                    scope_hash,
                    prompt_text,
                    embedding.encode(),
                    response_json,
                    cost_nano,
                    stamp,
                    stamp,
                ),
            )
            await self.db.commit()
            return cur.lastrowid

    async def refresh_cache_entry(self, entry_id: int, response_json: str, cost_nano: int, now: datetime) -> None:
        async with self._write_lock:
            await self.db.execute(
                "UPDATE cache_entries SET response_json = ?, cost_nano = ?, created_at = ?, last_used_at = ? WHERE id = ?",
                (response_json, cost_nano, _iso(now), _iso(now), entry_id),
            )
            await self.db.commit()

    async def trim_cache(self, virtual_key: str, max_entries: int, expired_before: datetime) -> list[int]:
        """Delete expired entries, then the least recently used ones beyond `max_entries`.
        Returns the ids removed so the in-memory index can forget them."""
        async with self._write_lock:
            cur = await self.db.execute(
                "SELECT id FROM cache_entries WHERE virtual_key = ? AND created_at < ?",
                (virtual_key, _iso(expired_before)),
            )
            doomed = [row["id"] for row in await cur.fetchall()]
            cur = await self.db.execute(
                "SELECT id FROM cache_entries WHERE virtual_key = ? AND created_at >= ? "
                "ORDER BY COALESCE(last_used_at, created_at) DESC, id DESC LIMIT -1 OFFSET ?",
                (virtual_key, _iso(expired_before), max_entries),
            )
            doomed += [row["id"] for row in await cur.fetchall()]
            if doomed:
                marks = ", ".join("?" for _ in doomed)
                await self.db.execute(f"DELETE FROM cache_entries WHERE id IN ({marks})", doomed)
                await self.db.commit()
            return doomed

    async def clear_cache(self, virtual_key: str | None) -> int:
        async with self._write_lock:
            if virtual_key:
                cur = await self.db.execute("DELETE FROM cache_entries WHERE virtual_key = ?", (virtual_key,))
            else:
                cur = await self.db.execute("DELETE FROM cache_entries")
            await self.db.commit()
            return cur.rowcount

    async def cache_stats(self, virtual_key: str | None) -> list[dict]:
        """Per key: lookups that hit, lookups that missed, live entries, and money saved by hits."""
        where, params = ("WHERE virtual_key = ?", [virtual_key]) if virtual_key else ("", [])
        cur = await self.db.execute(
            f"""SELECT virtual_key, SUM(cache = 'hit') AS hits, SUM(cache = 'miss') AS misses
                FROM request_logs {where + (" AND" if where else "WHERE")} cache IN ('hit', 'miss')
                GROUP BY virtual_key""",
            params,
        )
        stats = {row["virtual_key"]: {"hits": row["hits"], "misses": row["misses"]} for row in await cur.fetchall()}
        cur = await self.db.execute(
            f"""SELECT virtual_key, COUNT(*) AS entries, COALESCE(SUM(hit_count * cost_nano), 0) AS saved_nano
                FROM cache_entries {where} GROUP BY virtual_key""",
            params,
        )
        for row in await cur.fetchall():
            stats.setdefault(row["virtual_key"], {"hits": 0, "misses": 0}).update(
                entries=row["entries"], saved_nano=row["saved_nano"]
            )
        return [
            {"virtual_key": k, "entries": 0, "saved_nano": 0, **v}
            for k, v in sorted(stats.items(), key=lambda kv: kv[0])
        ]

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

    async def keys_overview(self, month: str) -> list[dict]:
        """Every key with its limits and this month's usage (for the console's usage-by-key table)."""
        cur = await self.db.execute(
            """
            SELECT k.virtual_key, k.team, k.status, k.monthly_budget_nano, k.rpm, k.tpm, k.model_allowlist,
                   k.cache_enabled, k.cache_threshold,
                   COALESCE(u.requests, 0) AS requests, COALESCE(u.prompt_tokens, 0) AS prompt_tokens,
                   COALESCE(u.completion_tokens, 0) AS completion_tokens, COALESCE(u.cost_nano, 0) AS cost_nano,
                   COALESCE(u.cache_hits, 0) AS cache_hits
            FROM virtual_keys k
            LEFT JOIN usage_monthly u ON u.virtual_key = k.virtual_key AND u.month = ?
            ORDER BY k.team
            """,
            (month,),
        )
        return [dict(row) for row in await cur.fetchall()]

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