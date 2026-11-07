import httpx
import pytest

from app.health import ProviderHealth
from app.main import app
from app.providers import ProviderRegistry
from app.providers.fake import FakeAdapter
from app.upstream import RetryPolicy, Upstream


async def no_sleep(_seconds):
    return None


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway database file, never the real prism.db."""
    monkeypatch.setenv("PRISM_DB_PATH", str(tmp_path / "test.db"))


@pytest.fixture
def fakes():
    return {"alpha": FakeAdapter("alpha"), "beta": FakeAdapter("beta")}


def install_fakes(fakes):
    """Put fake providers (fast, deterministic retries) behind the running app."""
    registry = ProviderRegistry(dict(fakes))
    app.state.providers = registry
    app.state.health = ProviderHealth()
    app.state.upstream = Upstream(
        registry, app.state.health, RetryPolicy(initial_backoff_ms=1), sleep=no_sleep, rng=lambda: 0.5
    )


@pytest.fixture
async def cache_client(fakes):
    """The real app (real startup, real SQLite) with fake providers, and the cache exactly as seeded:
    on for the search and free-tier keys, off for research and budget-demo."""
    async with app.router.lifespan_context(app):
        install_fakes(fakes)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
            yield c


@pytest.fixture
async def client(fakes):
    """Like cache_client but with the cache switched off for every key, so tests about routing, limits,
    budgets, failover and streaming can repeat the same prompt without it being served from the cache."""
    async with app.router.lifespan_context(app):
        install_fakes(fakes)
        await app.state.db.execute("UPDATE virtual_keys SET cache_enabled = 0")
        await app.state.db.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
            yield c