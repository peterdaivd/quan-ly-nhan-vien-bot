from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from app.models import CommissionType


def parse_integer(text: str, field_name: str, *, allow_zero: bool = False) -> int:
    compact = text.strip().replace(".", "").replace(",", "")
    if not re.fullmatch(r"\d+", compact):
        raise ValueError(f"{field_name} phải là số nguyên hợp lệ.")
    value = int(compact)
    if value < 0 or (value == 0 and not allow_zero):
        operator = "không âm" if allow_zero else "lớn hơn 0"
        raise ValueError(f"{field_name} phải {operator}.")
    return value


def parse_commission(text: str) -> tuple[CommissionType, int]:
    raw = text.strip()
    if raw.endswith("%"):
        number = raw[:-1].strip().replace(",", ".")
        try:
            percent = Decimal(number)
        except InvalidOperation as exc:
            raise ValueError("Hoa hồng phần trăm không hợp lệ.") from exc
        if percent < 0 or percent > 100:
            raise ValueError("Hoa hồng phần trăm phải từ 0% đến 100%.")
        basis_points = int((percent * 100).quantize(Decimal("1")))
        return CommissionType.PERCENT, basis_points
    return CommissionType.FIXED_PER_ITEM, parse_integer(raw, "Hoa hồng", allow_zero=True)


def parse_employee(text: str) -> tuple[int, str]:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 2:
        raise ValueError("Hãy nhập theo mẫu: Telegram ID | Tên nhân viên")
    telegram_id = parse_integer(parts[0], "Telegram ID")
    if telegram_id > 9_223_372_036_854_775_807:
        raise ValueError("Telegram ID vượt quá giới hạn hợp lệ.")
    if not parts[1]:
        raise ValueError("Tên nhân viên không được để trống.")
    return telegram_id, parts[1]
