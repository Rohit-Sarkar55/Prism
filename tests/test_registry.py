import pytest

from app.config import get_settings
from app.providers import ProviderRegistry, UnknownModelError
from app.providers.fake import FakeAdapter
from app.seed import load_gateway_config

CONFIG = load_gateway_config(get_settings().data_dir / "gateway_config.sample.json")


async def test_registry_builds_both_providers_from_config():
    registry = ProviderRegistry.from_config(CONFIG, timeout_s=1.0)
    try:
        assert set(registry.adapters) == {"alpha", "beta"}
        assert registry.provider_for("alpha-small").name == "alpha"
        assert registry.provider_for("alpha-large").name == "alpha"
        assert registry.provider_for("beta-small").name == "beta"
        assert registry.provider_for("beta-large").name == "beta"
    finally:
        await registry.aclose()


def test_unknown_model_raises():
    registry = ProviderRegistry({"alpha": FakeAdapter("alpha")})
    with pytest.raises(UnknownModelError):
        registry.provider_for("gamma-small")


def test_explicit_model_list_overrides_the_naming_rule():
    registry = ProviderRegistry({"alpha": FakeAdapter("alpha"), "beta": FakeAdapter("beta")}, {"alpha-small": "beta"})
    assert registry.provider_for("alpha-small").name == "beta"


def test_longest_provider_name_wins():
    registry = ProviderRegistry({"alpha": FakeAdapter("alpha"), "alpha-eu": FakeAdapter("alpha-eu")})
    assert registry.provider_for("alpha-eu-small").name == "alpha-eu"