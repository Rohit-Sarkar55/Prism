"""Money helpers. Cost is stored as integer nano-USD (1 USD = 1_000_000_000 nano).

Why: floats drift when summed. The load test adds up x-prism-cost-usd headers on the client
and compares them with our usage API, so both sides must add the same exact numbers.
"""

from decimal import Decimal

NANO_PER_USD = 1_000_000_000


def usd_to_nano(usd: Decimal | int | str) -> int:
    return int((Decimal(str(usd)) * NANO_PER_USD).to_integral_value())


def nano_to_usd_str(nano: int) -> str:
    """Exact decimal string with 9 places, e.g. 10950 -> '0.000010950'."""
    return f"{Decimal(nano) / NANO_PER_USD:.9f}"


def format_usd(nano: int) -> str:
    """Readable dollars for messages: 5_000_000_000 -> '5.00', 10_000 -> '0.00001'."""
    text = f"{Decimal(nano) / NANO_PER_USD:.9f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"