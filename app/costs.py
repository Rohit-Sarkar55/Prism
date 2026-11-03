"""Cost from provider-reported token usage and the price table.

Prices are USD per 1M tokens, so one token costs `price / 1_000_000` USD, which is
`price * 1000` nano-USD. With the table's two-decimal prices that is an exact integer.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.providers import Usage


def compute_cost_nano(pricing: dict[str, dict[str, Decimal]], model: str, usage: Usage) -> int:
    price = pricing[model]  # a KeyError here is a config bug: aliases are validated at startup
    nano = usage.prompt_tokens * price["input_per_1m"] * 1000 + usage.completion_tokens * price["output_per_1m"] * 1000
    return int(nano.to_integral_value(rounding=ROUND_HALF_UP))