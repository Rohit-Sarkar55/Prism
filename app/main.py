from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_schema, open_db
from app.providers import ProviderRegistry
from app.seed import load_gateway_config, load_pricing, seed_keys, validate_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.pricing = load_pricing(settings.data_dir / "model_pricing.json")
    app.state.config = load_gateway_config(settings.data_dir / "gateway_config.sample.json")
    validate_config(app.state.pricing, app.state.config)

    app.state.providers = ProviderRegistry.from_config(app.state.config, timeout_s=settings.provider_timeout_s)
    app.state.db = await open_db(settings.db_path)
    await init_schema(app.state.db)
    await seed_keys(app.state.db, settings.data_dir / "seed_keys.json")
    try:
        yield
    finally:
        await app.state.providers.aclose()
        await app.state.db.close()


app = FastAPI(title="Prism", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}