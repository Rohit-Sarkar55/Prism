"""Turn the model name a caller sent into an ordered chain of concrete provider models."""

from dataclasses import dataclass

from app.errors import not_found


@dataclass(frozen=True)
class Route:
    requested: str  # exactly what the caller sent
    alias: str | None  # alias actually used (for `auto`, the tier it picked)
    chain: list[str]  # primary first, then fallbacks in order
    reason: str | None  # why `auto` picked this tier (None for plain aliases)


def route_auto(config: dict, messages: list[dict]) -> tuple[str, str]:
    """PLACEHOLDER until Phase 6: always picks the simple tier and says so in the log."""
    tiers = config["model_aliases"]["auto"]["route_by_difficulty"]
    return tiers["simple"], "auto router not implemented yet; defaulted to the simple tier"


def resolve_route(config: dict, pricing: dict, requested: str, messages: list[dict]) -> Route:
    aliases = config["model_aliases"]
    if requested in aliases:
        spec = aliases[requested]
        reason = None
        if "primary" not in spec:  # `auto`: classify first, then reuse normal alias resolution
            requested_alias, reason = route_auto(config, messages)
            spec = aliases[requested_alias]
        else:
            requested_alias = requested
        return Route(requested, requested_alias, [spec["primary"], *spec.get("fallbacks", [])], reason)
    if requested in pricing:  # a concrete model name, e.g. "alpha-small"
        return Route(requested, None, [requested], None)
    raise not_found(requested)