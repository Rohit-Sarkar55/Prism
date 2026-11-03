from decimal import Decimal

from app.money import nano_to_usd_str, usd_to_nano


def test_usd_to_nano_is_exact():
    assert usd_to_nano(Decimal("0.00001")) == 10_000
    assert usd_to_nano(50) == 50_000_000_000
    assert usd_to_nano("0.15") == 150_000_000


def test_nano_to_usd_str_has_nine_places():
    assert nano_to_usd_str(10_950) == "0.000010950"
    assert nano_to_usd_str(0) == "0.000000000"