from decimal import Decimal

from app.utils.money import format_commission, format_decimal


def test_format_decimal_never_uses_scientific_notation() -> None:
    assert format_decimal(Decimal("1E+1")) == "10"
    assert format_decimal(Decimal("5E+0")) == "5"
    assert format_decimal(Decimal("12.5")) == "12.5"
    assert format_decimal(Decimal("10.0")) == "10"
    assert format_decimal(Decimal("0")) == "0"


def test_format_commission() -> None:
    assert format_commission("PERCENT", Decimal("1E+1")) == "10%"
    assert format_commission("PERCENT", Decimal("5E+0")) == "5%"
    assert format_commission("PERCENT", Decimal("12.5")) == "12.5%"
    assert format_commission("FIXED_PER_ITEM", Decimal("5000")) == "5.000đ/SP"
    # Database hiện lưu phần trăm theo basis points: 10% = 1000.
    assert format_commission("PERCENT", 1000, percent_is_basis_points=True) == "10%"
