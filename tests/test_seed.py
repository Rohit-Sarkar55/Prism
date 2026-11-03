import json
from decimal import Decimal

import pytest

from app.config import get_settings
from app.db import init_schema, open_db
from app.seed import load_gateway_config, load_pricing, seed_keys, validate_config

DATA = get_settings().data_dir


@pytest.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / "seed.db")
    await init_schema(conn)
    yield conn
    await conn.close()


def test_pricing_loads_exact_decimals():
    pricing = load_pricing(DATA / "model_pricing.json")
    assert set(pricing) == {"alpha-small", "alpha-large", "beta-small", "beta-large"}
    assert pricing["alpha-small"]["input_per_1m"] == Decimal("0.15")
    assert "_comment" not in pricing


def test_gateway_config_is_consistent_with_pricing():
    pricing = load_pricing(DATA / "model_pricing.json")
    config = load_gateway_config(DATA / "gateway_config.sample.json")
    validate_config(pricing, config)  # must not raise
    assert config["model_aliases"]["fast"]["primary"] == "alpha-small"


def test_validate_config_rejects_unpriced_model():
    pricing = load_pricing(DATA / "model_pricing.json")
    config = load_gateway_config(DATA / "gateway_config.sample.json")
    config["model_aliases"]["fast"]["fallbacks"] = ["ghost-model"]
    with pytest.raises(ValueError, match="ghost-model"):
        validate_config(pricing, config)


async def test_seed_keys_loads_four_tenants_with_exact_values(db):
    assert await seed_keys(db, DATA / "seed_keys.json") == 4
    cur = await db.execute("SELECT * FROM virtual_keys WHERE virtual_key = ?", ("prism-sk-free-7g8h9i",))
    row = await cur.fetchone()
    assert row["team"] == "free-tier"
    assert row["rpm"] == 10
    assert row["monthly_budget_nano"] == 5_000_000_000
    assert json.loads(row["model_allowlist"]) == ["fast"]
    assert row["cache_enabled"] == 1 and row["cache_threshold"] == 0.85


async def test_budget_demo_key_has_tiny_budget_and_no_cache(db):
    await seed_keys(db, DATA / "seed_keys.json")
    cur = await db.execute("SELECT * FROM virtual_keys WHERE virtual_key = ?", ("prism-sk-budget-demo-0j1k2l",))
    row = await cur.fetchone()
    assert row["monthly_budget_nano"] == 10_000  # $0.00001
    assert row["cache_enabled"] == 0 and row["cache_threshold"] is None


async def test_reseeding_is_idempotent_and_keeps_usage(db):
    await seed_keys(db, DATA / "seed_keys.json")
    await db.execute(
        "INSERT INTO usage_monthly (virtual_key, month, requests, cost_nano) VALUES (?, ?, 3, 777)",
        ("prism-sk-search-1a2b3c", "2026-11"),
    )
    await db.commit()

    await seed_keys(db, DATA / "seed_keys.json")  # simulate a restart

    cur = await db.execute("SELECT COUNT(*) AS n FROM virtual_keys")
    assert (await cur.fetchone())["n"] == 4
    cur = await db.execute("SELECT cost_nano FROM usage_monthly WHERE virtual_key = 'prism-sk-search-1a2b3c'")
    assert (await cur.fetchone())["cost_nano"] == 777


async def test_wal_mode_is_enabled(db):
    cur = await db.execute("PRAGMA journal_mode")
    assert (await cur.fetchone())[0] == "wal"