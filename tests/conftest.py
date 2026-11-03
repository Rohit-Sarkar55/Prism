import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway database file, never the real prism.db."""
    monkeypatch.setenv("PRISM_DB_PATH", str(tmp_path / "test.db"))