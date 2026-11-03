from decimal import Decimal

from app.costs import compute_cost_nano
from app.providers import Usage

PRICING = {
    "alpha-small": {"input_per_1m": Decimal("0.15"), "output_per_1m": Decimal("0.60")},
    "alpha-large": {"input_per_1m": Decimal("3.00"), "output_per_1m": Decimal("15.00")},
}


def test_small_model_cost_is_exact():
    # 5 prompt tokens * 150 nano + 17 completion tokens * 600 nano
    assert compute_cost_nano(PRICING, "alpha-small", Usage(5, 17)) == 10_950


def test_one_million_tokens_cost_the_listed_price():
    assert compute_cost_nano(PRICING, "alpha-large", Usage(1_000_000, 0)) == 3_000_000_000  # $3.00
    assert compute_cost_nano(PRICING, "alpha-large", Usage(0, 1_000_000)) == 15_000_000_000  # $15.00


def test_zero_tokens_cost_nothing():
    assert compute_cost_nano(PRICING, "alpha-small", Usage(0, 0)) == 0


def test_fractional_nano_rounds_half_up():
    pricing = {"m": {"input_per_1m": Decimal("0.0005"), "output_per_1m": Decimal(0)}}  # 0.5 nano/token
    assert compute_cost_nano(pricing, "m", Usage(1, 0)) == 1
    assert compute_cost_nano(pricing, "m", Usage(3, 0)) == 2  # 1.5 -> 2