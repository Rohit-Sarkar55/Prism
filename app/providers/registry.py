"""Builds adapters from gateway_config.json and answers: which provider serves this model?"""

from app.providers.base import ProviderAdapter
from app.providers.openai_compat import OpenAICompatAdapter


class UnknownModelError(KeyError):
    pass


class ProviderRegistry:
    """Model -> provider rule: a provider may list its models explicitly (`models` in config).
    Otherwise it serves every model named `<provider>-...`, which is how the provided mocks
    work (alpha serves alpha-small and alpha-large).
    """

    def __init__(self, adapters: dict[str, ProviderAdapter], explicit_models: dict[str, str] | None = None):
        self.adapters = adapters
        self._explicit = explicit_models or {}

    @classmethod
    def from_config(cls, config: dict, *, timeout_s: float) -> "ProviderRegistry":
        adapters: dict[str, ProviderAdapter] = {}
        explicit: dict[str, str] = {}
        for p in config["providers"]:
            adapters[p["name"]] = OpenAICompatAdapter(p["name"], p["base_url"], p["api_key"], timeout_s=timeout_s)
            for model in p.get("models", []):
                explicit[model] = p["name"]
        return cls(adapters, explicit)

    def provider_for(self, model: str) -> ProviderAdapter:
        if model in self._explicit:
            return self.adapters[self._explicit[model]]
        matches = [name for name in self.adapters if model.startswith(f"{name}-")]
        if not matches:
            raise UnknownModelError(model)
        return self.adapters[max(matches, key=len)]  # longest provider name wins

    async def aclose(self) -> None:
        for adapter in self.adapters.values():
            await adapter.aclose()