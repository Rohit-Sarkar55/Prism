import httpx
import pytest

from app.main import app
from app.providers import ProviderRegistry
from app.providers.fake import FakeAdapter


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway database file, never the real prism.db."""
    monkeypatch.setenv("PRISM_DB_PATH", str(tmp_path / "test.db"))


@pytest.fixture
def fakes():
    return {"alpha": FakeAdapter("alpha"), "beta": FakeAdapter("beta")}


@pytest.fixture
async def client(fakes):
    """The real app (real startup, real SQLite) with fake providers behind the registry."""
    async with app.router.lifespan_context(app):
        app.state.providers = ProviderRegistry(dict(fakes))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
            yield c