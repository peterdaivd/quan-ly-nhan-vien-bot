from decimal import Decimal, ROUND_HALF_UP


def format_money(value: int) -> str:
    return f"{value:,}".replace(",", ".") + "đ"


def format_decimal(value) -> str:
    """Decimal ở dạng thường, không bao giờ trả scientific notation."""
    decimal_value = Decimal(str(value))
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_commission(commission_type, commission_value, *, percent_is_basis_points: bool = False) -> str:
    type_value = getattr(commission_type, "value", commission_type)
    value = Decimal(str(commission_value))
    if type_value == "PERCENT":
        # Schema hiện tại lưu 10% dưới dạng 1000 basis points.
        if percent_is_basis_points:
            value /= Decimal(100)
        return f"{format_decimal(value)}%"
    amount = int(value)
    return f"{amount:,}".replace(",", ".") + "đ/SP"


def percent_label(basis_points: int) -> str:
    """Tương thích code cũ; màn hình mới nên gọi format_commission()."""
    return format_commission("PERCENT", basis_points, percent_is_basis_points=True)


def round_vnd(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

