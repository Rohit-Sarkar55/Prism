"""Runtime settings, overridable with PRISM_* environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRISM_", env_file=".env", extra="ignore")

    db_path: Path = PROJECT_ROOT / "prism.db"
    data_dir: Path = PROJECT_ROOT / "data"
    admin_token: str = "demo-admin-token"
    # Upstream timeout (seconds). Low on purpose so the demo's 3s "slow provider" trips it;
    # real providers usually need a higher value.
    provider_timeout_s: float = 2.0
    # Upper bound for retries plus failover on one request.
    upstream_deadline_s: float = 10.0


def get_settings() -> Settings:
    # Deliberately not cached: tests change the environment between runs.
    return Settings()