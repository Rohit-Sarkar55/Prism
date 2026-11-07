"""SQLite connection and schema. WAL mode lets readers proceed while a writer commits."""

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS virtual_keys (
    virtual_key         TEXT PRIMARY KEY,
    team                TEXT NOT NULL,
    monthly_budget_nano INTEGER NOT NULL,
    rpm                 INTEGER NOT NULL,
    tpm                 INTEGER,
    model_allowlist     TEXT NOT NULL,          -- JSON array of aliases / model names
    cache_enabled       INTEGER NOT NULL,       -- 0 or 1
    cache_threshold     REAL,                   -- NULL when the cache is disabled
    status              TEXT NOT NULL DEFAULT 'active',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_monthly (
    virtual_key       TEXT NOT NULL REFERENCES virtual_keys(virtual_key),
    month             TEXT NOT NULL,            -- 'YYYY-MM' (calendar month, reset-on-read)
    requests          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_nano         INTEGER NOT NULL DEFAULT 0,
    cache_hits        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (virtual_key, month)
);

CREATE TABLE IF NOT EXISTS request_logs (
    request_id        TEXT PRIMARY KEY,
    virtual_key       TEXT,                     -- NULL when the key was missing or unknown
    requested_model   TEXT,
    resolved_provider TEXT,
    resolved_model    TEXT,
    status            TEXT NOT NULL,            -- ok, cache_hit, rejected_auth, ...
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_nano         INTEGER NOT NULL DEFAULT 0,
    cache             TEXT NOT NULL DEFAULT 'bypass',  -- hit | miss (looked up, no match) | bypass (not consulted)
    cache_similarity  REAL,                     -- score of the matched entry, for cache hits
    fallback          INTEGER NOT NULL DEFAULT 0,
    route_reason      TEXT,
    retries           INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER,                  -- total time inside the gateway
    upstream_ms       INTEGER,                  -- time spent waiting on the provider
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_key_time ON request_logs (virtual_key, created_at DESC);

CREATE TABLE IF NOT EXISTS cache_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_key   TEXT NOT NULL REFERENCES virtual_keys(virtual_key),
    model         TEXT NOT NULL,                -- alias the entry was created under (as the caller sent it)
    scope_hash    TEXT NOT NULL DEFAULT '',     -- hash of the request parameters and the earlier conversation
    prompt_text   TEXT NOT NULL,                -- first 200 chars of the last user message, for debugging only
    embedding     BLOB NOT NULL,                -- the matcher's JSON representation (see app/embedding.py)
    response_json TEXT NOT NULL,                -- {content, model, finish_reason}
    cost_nano     INTEGER NOT NULL DEFAULT 0,   -- what the original upstream call cost: "saved" per later hit
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    hit_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_scope ON cache_entries (virtual_key, model);
"""


async def open_db(path: Path | str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, ddl: str) -> None:
    """Tiny migration: add a column to a table created by an older version of the schema."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    if column not in {row["name"] for row in await cur.fetchall()}:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def init_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    await _ensure_column(db, "request_logs", "upstream_ms", "INTEGER")
    await _ensure_column(db, "request_logs", "cache_similarity", "REAL")
    await _ensure_column(db, "cache_entries", "scope_hash", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(db, "cache_entries", "cost_nano", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(db, "cache_entries", "last_used_at", "TEXT")
    # created after the migrations above, because an older database lacks the scope_hash column
    await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_lookup ON cache_entries (virtual_key, model, scope_hash)")
    await db.commit()