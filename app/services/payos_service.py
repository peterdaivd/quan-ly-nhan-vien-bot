from __future__ import annotations

from datetime import datetime
from typing import Any

from payos import AsyncPayOS
from payos.types import CreatePaymentLinkRequest

from app.config import Settings
from app.models import AdminPayment, Deposit


def create_payos_client(settings: Settings) -> AsyncPayOS:
    return AsyncPayOS(
        client_id=settings.payos_client_id,
        api_key=settings.payos_api_key,
        checksum_key=settings.payos_checksum_key,
    )


def payos_is_configured(settings: Settings) -> bool:
    return all((
        settings.payos_client_id,
        settings.payos_api_key,
        settings.payos_checksum_key,
        settings.payos_webhook_url,
        settings.payos_return_url,
        settings.payos_cancel_url,
    ))


async def create_payment_link(client: AsyncPayOS, settings: Settings, deposit: Deposit):
    request = CreatePaymentLinkRequest(
        order_code=deposit.order_code,
        amount=deposit.amount,
        description=f"NAP{deposit.id}",
        return_url=settings.payos_return_url,
        cancel_url=settings.payos_cancel_url,
        expired_at=int(deposit.expires_at.timestamp()),
    )
    return await client.payment_requests.create(payment_data=request)


async def create_admin_payment_link(client: AsyncPayOS, settings: Settings, payment: AdminPayment):
    request = CreatePaymentLinkRequest(
        order_code=payment.order_code,
        amount=payment.amount,
        description=f"ADMIN{payment.admin_user_id}",
        return_url=settings.payos_return_url,
        cancel_url=settings.payos_cancel_url,
        expired_at=int((datetime.now().timestamp()) + settings.payos_payment_expires_minutes * 60),
    )
    return await client.payment_requests.create(payment_data=request)


async def get_payment(client: AsyncPayOS, deposit: Deposit):
    return await client.payment_requests.get(deposit.order_code)


async def cancel_payment(client: AsyncPayOS, deposit: Deposit):
    return await client.payment_requests.cancel(
        deposit.order_code, cancellation_reason="Người dùng hủy trên Telegram"
    )


def payment_data_dict(data: Any) -> dict[str, Any]:
    if hasattr(data, "model_dump"):
        return data.model_dump(by_alias=True, mode="json")
    return dict(data)
