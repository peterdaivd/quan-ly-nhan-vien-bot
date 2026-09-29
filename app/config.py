from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    database_url: str
    payment_mode: str
    payos_client_id: str
    payos_api_key: str
    payos_checksum_key: str
    payos_webhook_url: str
    payos_return_url: str
    payos_cancel_url: str
    payos_payment_expires_minutes: int
    api_host: str
    api_port: int
    super_admin_ids: frozenset[int] = frozenset()


def load_settings(*, require_token: bool = True) -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise RuntimeError("BOT_TOKEN chưa được cấu hình trong file .env")

    admin_ids: set[int] = set()
    raw_ids = os.getenv("SUPER_ADMIN_IDS", os.getenv("ADMIN_IDS", ""))
    for item in raw_ids.split(","):
        item = item.strip()
        if not item:
            continue
        if not item.isdigit() or int(item) <= 0:
            raise RuntimeError(f"ADMIN_IDS chứa Telegram ID không hợp lệ: {item!r}")
        admin_ids.add(int(item))
    if require_token and not admin_ids:
        raise RuntimeError("ADMIN_IDS chưa có ít nhất một Telegram ID hợp lệ")

    expiry = int(os.getenv("PAYOS_PAYMENT_EXPIRES_MINUTES", "30"))
    if expiry <= 0:
        raise RuntimeError("PAYOS_PAYMENT_EXPIRES_MINUTES phải lớn hơn 0")
    payment_mode = os.getenv("PAYMENT_MODE", "PAYOS").strip()
    if payment_mode != "PAYOS":
        raise RuntimeError("Project hiện chỉ hỗ trợ PAYMENT_MODE=PAYOS")
    payos_values = {
        "PAYOS_CLIENT_ID": os.getenv("PAYOS_CLIENT_ID", "").strip(),
        "PAYOS_API_KEY": os.getenv("PAYOS_API_KEY", "").strip(),
        "PAYOS_CHECKSUM_KEY": os.getenv("PAYOS_CHECKSUM_KEY", "").strip(),
        "PAYOS_WEBHOOK_URL": os.getenv("PAYOS_WEBHOOK_URL", "").strip(),
        "PAYOS_RETURN_URL": os.getenv("PAYOS_RETURN_URL", "").strip(),
        "PAYOS_CANCEL_URL": os.getenv("PAYOS_CANCEL_URL", "").strip(),
    }
    if require_token:
        missing = [name for name, value in payos_values.items() if not value]
        if missing:
            raise RuntimeError("Thiếu cấu hình payOS trong .env: " + ", ".join(missing))
        for name in ("PAYOS_WEBHOOK_URL", "PAYOS_RETURN_URL", "PAYOS_CANCEL_URL"):
            if not payos_values[name].startswith("https://"):
                raise RuntimeError(f"{name} phải là URL HTTPS công khai")

    return Settings(
        bot_token=token,
        admin_ids=frozenset(admin_ids),
        database_url=os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:///employee_bot.db"
        ).strip(),
        payment_mode=payment_mode,
        payos_client_id=payos_values["PAYOS_CLIENT_ID"],
        payos_api_key=payos_values["PAYOS_API_KEY"],
        payos_checksum_key=payos_values["PAYOS_CHECKSUM_KEY"],
        payos_webhook_url=payos_values["PAYOS_WEBHOOK_URL"],
        payos_return_url=payos_values["PAYOS_RETURN_URL"],
        payos_cancel_url=payos_values["PAYOS_CANCEL_URL"],
        payos_payment_expires_minutes=expiry,
        api_host=os.getenv("API_HOST", "0.0.0.0").strip(),
        api_port=int(os.getenv("API_PORT", "8080")),
        super_admin_ids=frozenset(admin_ids),
    )
