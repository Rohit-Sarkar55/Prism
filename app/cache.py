"""Semantic response cache: what may be cached, how an entry is found, and when it goes away.

SCOPE (the security-critical part). An entry can only be served to a request that matches ALL of:
  * the same virtual key           - tenants never share entries (a cross-tenant hit is a data leak)
  * the same model/alias as sent   - a `smart` caller never receives a `fast` answer; `auto` is its own scope
  * the same parameters            - temperature, max_tokens, stop, ... change the answer
  * the same earlier conversation  - system prompt and prior turns must match EXACTLY
Only then is the LAST USER MESSAGE compared by similarity (see embedding.py) against the key's threshold.
Scope fields are exact-match filters, never similarity: similarity is never allowed to bridge a scope boundary.

WHAT IS NEVER CACHED: error responses, partial streams (provider died or client left), time-sensitive prompts
(re-asking "what is the current status" must hit the system again), prompts over 4000 characters (they rarely
repeat, and long text makes word-set matching unreliable), and anything for a key whose cache is switched off.

LIFECYCLE: entries expire after a TTL (default 24 h) and each key keeps at most N entries (default 500);
over the limit the least recently used go first. Entries persist in SQLite across restarts; a per-scope
in-memory index is loaded lazily so a lookup is a handful of set comparisons, not a database scan.

PRIVACY: an entry holds the full model response (it must, to be served) and the first 200 characters of the
prompt (debugging only). The request log itself stores no prompt or response bodies.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.difficulty import last_user_text
from app.embedding import Embedding, LexicalEmbedder, is_time_sensitive
from app.store import KeyRecord, Store

logger = logging.getLogger("prism.cache")

DEFAULT_THRESHOLD = 0.9  # used only if a key enables the cache without giving a threshold
PREVIEW_CHARS = 200
DUPLICATE_SIMILARITY = 0.999


@dataclass(frozen=True)
class CachePlan:
    """Everything decided about caching for one request, before the upstream call."""

    virtual_key: str
    alias: str  # the model/alias exactly as the caller sent it
    scope_hash: str
    embedding: Embedding
    threshold: float
    preview: str


@dataclass(frozen=True)
class CacheHit:
    entry_id: int
    content: str
    model: str  # the model that originally produced it
    finish_reason: str
    similarity: float


@dataclass
class _Indexed:
    id: int
    embedding: Embedding
    created_at: float  # epoch seconds


def completion_body(hit: CacheHit) -> dict:
    """The OpenAI-shaped body for a cache hit. Usage is zero: nothing was consumed upstream, so
    tokens x price still equals cost for every request in the usage API."""
    return {
        "id": f"chatcmpl-cache-{hit.entry_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": hit.model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": hit.content}, "finish_reason": hit.finish_reason}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _normalise(content) -> str:
    if isinstance(content, str):
        return " ".join(content.split())
    return json.dumps(content, sort_keys=True, default=str)


class SemanticCache:
    def __init__(
        self,
        store: Store,
        embedder: LexicalEmbedder | None = None,
        *,
        ttl_s: float = 86_400,
        max_entries_per_key: int = 500,
        max_prompt_chars: int = 4_000,
        clock=time.time,
    ):
        self.store = store
        self.embedder = embedder or LexicalEmbedder()
        self.ttl_s = ttl_s
        self.max_entries_per_key = max_entries_per_key
        self.max_prompt_chars = max_prompt_chars
        self._clock = clock
        self._index: dict[tuple[str, str, str], list[_Indexed]] = {}
        self._load_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()

    # ---- decide ---------------------------------------------------------------------------

    def plan(self, key: KeyRecord, alias: str, messages: list[dict], params: dict) -> CachePlan | None:
        """None means "do not use the cache for this request" (nothing is looked up or stored)."""
        if not key.cache_enabled or not messages or messages[-1].get("role") != "user":
            return None
        text = last_user_text(messages[-1:])
        if not text.strip() or len(text) > self.max_prompt_chars or is_time_sensitive(text):
            return None
        earlier = [(m.get("role"), _normalise(m.get("content"))) for m in messages[:-1]]
        scope = json.dumps({"params": params, "earlier": earlier}, sort_keys=True, default=str)
        return CachePlan(
            virtual_key=key.virtual_key,
            alias=alias,
            scope_hash=hashlib.sha256(scope.encode()).hexdigest(),
            embedding=self.embedder.embed(text),
            threshold=key.cache_threshold if key.cache_threshold is not None else DEFAULT_THRESHOLD,
            preview=text[:PREVIEW_CHARS],
        )

    # ---- find -----------------------------------------------------------------------------

    async def lookup(self, plan: CachePlan) -> CacheHit | None:
        entries = await self._scope(plan)
        now = self._clock()
        best: tuple[float, _Indexed] | None = None
        for entry in entries:
            if now - entry.created_at > self.ttl_s:
                continue
            score = self.embedder.similarity(plan.embedding, entry.embedding)
            if score >= plan.threshold and (best is None or score > best[0]):
                best = (score, entry)
        if best is None:
            return None
        row = await self.store.get_cache_entry(best[1].id)
        if row is None:  # trimmed between the index check and now
            self._forget({best[1].id})
            return None
        payload = json.loads(row["response_json"])
        return CacheHit(best[1].id, payload["content"], payload["model"], payload["finish_reason"], best[0])

    # ---- keep -----------------------------------------------------------------------------

    async def store_response(
        self, plan: CachePlan, *, content: str, model: str, finish_reason: str, cost_nano: int
    ) -> None:
        if not content or not content.strip():
            return
        now = datetime.fromtimestamp(self._clock(), UTC)
        payload = json.dumps({"content": content, "model": model, "finish_reason": finish_reason})
        async with self._store_lock:  # check-then-insert must not interleave, or concurrent twins duplicate
            entries = await self._scope(plan)
            twin = next(
                (
                    e
                    for e in entries
                    if e.embedding.text_key == plan.embedding.text_key
                    and self.embedder.similarity(e.embedding, plan.embedding) >= DUPLICATE_SIMILARITY
                ),
                None,
            )
            if twin:
                await self.store.refresh_cache_entry(twin.id, payload, cost_nano, now)
                twin.created_at = now.timestamp()
            else:
                entry_id = await self.store.insert_cache_entry(
                    plan.virtual_key, plan.alias, plan.scope_hash, plan.preview,
                    plan.embedding.to_json(), payload, cost_nano, now,
                )  # fmt: skip
                entries.append(_Indexed(entry_id, plan.embedding, now.timestamp()))
            removed = await self.store.trim_cache(
                plan.virtual_key, self.max_entries_per_key, now - timedelta(seconds=self.ttl_s)
            )
            if removed:
                self._forget(set(removed))

    async def clear(self, virtual_key: str | None = None) -> int:
        async with self._store_lock:
            deleted = await self.store.clear_cache(virtual_key)
            for scope in [s for s in self._index if virtual_key is None or s[0] == virtual_key]:
                del self._index[scope]
            return deleted

    # ---- internals ------------------------------------------------------------------------

    async def _scope(self, plan: CachePlan) -> list[_Indexed]:
        scope = (plan.virtual_key, plan.alias, plan.scope_hash)
        entries = self._index.get(scope)
        if entries is None:
            async with self._load_lock:
                entries = self._index.get(scope)
                if entries is None:
                    rows = await self.store.load_cache_scope(*scope)
                    entries = [
                        _Indexed(r["id"], Embedding.from_json(r["embedding"]), datetime.fromisoformat(r["created_at"]).timestamp())
                        for r in rows
                    ]  # fmt: skip
                    self._index[scope] = entries
        return entries

    def _forget(self, ids: set[int]) -> None:
        for entries in self._index.values():
            entries[:] = [e for e in entries if e.id not in ids]