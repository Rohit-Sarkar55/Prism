"""The x-prism-* header contract. Sent on every data-plane response, errors included."""

from app.money import nano_to_usd_str


def prism_headers(
    *,
    provider: str = "none",
    cache: str = "miss",
    fallback: bool = False,
    cost_nano: int = 0,
    request_id: str | None = None,
) -> dict[str, str]:
    headers = {
        "x-prism-provider": provider,  # e.g. "alpha/alpha-small", or "none" when nothing served it
        "x-prism-cache": cache,  # "hit" or "miss"
        "x-prism-fallback": "true" if fallback else "false",
        "x-prism-cost-usd": nano_to_usd_str(cost_nano) if cost_nano else "0",
    }
    if request_id:
        headers["x-prism-request-id"] = request_id
    return headers