from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminPayment, AdminPaymentStatus, DailySale, PaymentEvent, User
from app.services.deposit_service import generate_order_code
from app.services.shipping_service import get_shipping_total, vietnam_today


@dataclass(frozen=True)
class AdminPaymentResult:
    outcome: str
    admin_payment_id: int | None = None
    admin_telegram_id: int | None = None
    admin_name: str | None = None
    receiver_telegram_id: int | None = None
    outstanding: int | None = None
    amount: int | None = None
    reference: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class DailySettlement:
    settlement_date: date
    revenue: int
    commission: int
    shipping: int
    due: int
    paid: int
    remaining: int


@dataclass(frozen=True)
class SettlementTotals:
    start_date: date | None
    end_date: date | None
    revenue: int
    commission: int
    shipping: int
    due: int


async def get_payment_receiver(session: AsyncSession, user: User) -> User | None:
    if user.role.value == "SUPER_ADMIN":
        return None
    if user.manager_admin_id is not None:
        return await session.get(User, user.manager_admin_id)
    return await session.scalar(select(User).where(User.role == "SUPER_ADMIN", User.is_deleted.is_(False)).order_by(User.id))


async def personal_company_total(session: AsyncSession, user_id: int) -> int:
    company = int(await session.scalar(
        select(func.coalesce(func.sum(DailySale.company_amount), 0)).where(DailySale.user_id == user_id)
    ) or 0)
    return max(0, company - await get_shipping_total(session, user_id))


async def settlement_totals(
    session: AsyncSession, user_id: int,
    start_date: date | None = None, end_date: date | None = None,
) -> SettlementTotals:
    query = select(
        func.coalesce(func.sum(DailySale.revenue), 0),
        func.coalesce(func.sum(DailySale.commission_amount), 0),
    ).where(DailySale.user_id == user_id)
    if start_date is not None:
        query = query.where(DailySale.sale_date >= start_date)
    if end_date is not None:
        query = query.where(DailySale.sale_date <= end_date)
    revenue, commission = (await session.execute(query)).one()
    shipping = await get_shipping_total(session, user_id, start_date, end_date)
    due = max(0, int(revenue) - int(commission) - shipping)
    return SettlementTotals(start_date, end_date, int(revenue), int(commission), shipping, due)


async def paid_between(session: AsyncSession, payer_user_id: int, receiver_user_id: int) -> int:
    return int(await session.scalar(select(func.coalesce(func.sum(AdminPayment.amount), 0)).where(
        AdminPayment.payer_user_id == payer_user_id,
        AdminPayment.receiver_user_id == receiver_user_id,
        AdminPayment.status == AdminPaymentStatus.PAID,
    )) or 0)


async def received_from_staff_total(session: AsyncSession, admin_user_id: int) -> int:
    return int(await session.scalar(select(func.coalesce(func.sum(AdminPayment.amount), 0)).where(
        AdminPayment.receiver_user_id == admin_user_id,
        AdminPayment.status == AdminPaymentStatus.PAID,
    )) or 0)


async def amount_due_to_receiver(session: AsyncSession, user: User, receiver: User) -> int:
    gross = await personal_company_total(session, user.id)
    if user.role.value == "ADMIN":
        gross += await received_from_staff_total(session, user.id)
    return max(0, gross - await paid_between(session, user.id, receiver.id))


async def create_settlement(
    session: AsyncSession, payer_user_id: int, receiver_user_id: int, amount: int, *, payment_method: str,
    settlement_date: date | None = None,
) -> AdminPayment:
    if amount <= 0:
        raise ValueError("Hiện không có số tiền cần nộp.")
    item = AdminPayment(
        admin_user_id=payer_user_id,
        payer_user_id=payer_user_id,
        receiver_user_id=receiver_user_id,
        payment_method=payment_method,
        settlement_date=settlement_date or vietnam_today(),
        amount=amount,
        order_code=await generate_order_code(session),
        status=AdminPaymentStatus.PENDING,
    )
    session.add(item)
    await session.commit()
    return item


async def create_admin_payment(session: AsyncSession, admin_user_id: int, amount: int, receiver_user_id: int | None = None) -> AdminPayment:
    if receiver_user_id is None:
        receiver = await session.scalar(select(User).where(User.role == "SUPER_ADMIN", User.is_deleted.is_(False)).order_by(User.id))
        if receiver is None:
            raise ValueError("Không tìm thấy SUPER_ADMIN nhận tiền.")
        receiver_user_id = receiver.id
    return await create_settlement(session, admin_user_id, receiver_user_id, amount, payment_method="PAYOS")


async def confirm_manual_settlement(session: AsyncSession, receiver: User, payment_id: int) -> AdminPayment:
    item = await session.get(AdminPayment, payment_id)
    if not item or item.payment_method != "MANUAL" or item.status != AdminPaymentStatus.PENDING:
        raise ValueError("Giao dịch không còn chờ xác nhận.")
    payer = await session.get(User, item.payer_user_id)
    if item.receiver_user_id != receiver.id or not payer or payer.manager_admin_id != receiver.id:
        raise PermissionError("Bạn không có quyền xác nhận giao dịch này.")
    item.status = AdminPaymentStatus.PAID
    item.paid_at = datetime.now()
    item.payos_reference = f"MANUAL-{item.id}"
    await session.commit()
    return item


# Tương thích các màn hình/thử nghiệm cũ.
group_company_total = personal_company_total


async def paid_admin_total(session: AsyncSession, admin_user_id: int) -> int:
    receiver = await session.scalar(select(User).where(User.role == "SUPER_ADMIN", User.is_deleted.is_(False)).order_by(User.id))
    return await paid_between(session, admin_user_id, receiver.id) if receiver else 0


async def daily_settlement(session: AsyncSession, user: User, receiver: User, day: date) -> DailySettlement:
    personal = await settlement_totals(session, user.id, day, day)
    due = personal.due
    if user.role.value == "ADMIN":
        due += int(await session.scalar(select(func.coalesce(func.sum(AdminPayment.amount), 0)).where(
            AdminPayment.receiver_user_id == user.id, AdminPayment.status == AdminPaymentStatus.PAID,
            AdminPayment.settlement_date == day,
        )) or 0)
    paid = int(await session.scalar(select(func.coalesce(func.sum(AdminPayment.amount), 0)).where(
        AdminPayment.payer_user_id == user.id, AdminPayment.receiver_user_id == receiver.id,
        AdminPayment.status == AdminPaymentStatus.PAID, AdminPayment.settlement_date == day,
    )) or 0)
    return DailySettlement(
        day, personal.revenue, personal.commission, personal.shipping,
        due, paid, max(0, due - paid),
    )


async def mark_admin_link_created(session: AsyncSession, payment_id: int, link) -> AdminPayment:
    item = await session.get(AdminPayment, payment_id)
    if item is None:
        raise ValueError("Không tìm thấy yêu cầu thanh toán.")
    item.payos_payment_link_id = link.payment_link_id
    item.checkout_url = link.checkout_url
    item.qr_code = link.qr_code
    await session.commit()
    return item


async def process_successful_admin_payment(
    session: AsyncSession, *, order_code: int, amount: int, reference: str,
    payment_link_id: str, raw_data: dict[str, Any],
) -> AdminPaymentResult:
    item = await session.scalar(select(AdminPayment).where(AdminPayment.order_code == order_code))
    if item is None:
        return AdminPaymentResult(outcome="NOT_FOUND")
    if item.status == AdminPaymentStatus.PAID:
        return AdminPaymentResult(outcome="DUPLICATE", admin_payment_id=item.id, reference=reference)
    if item.status != AdminPaymentStatus.PENDING:
        return AdminPaymentResult(outcome="REJECTED", admin_payment_id=item.id)
    if amount != item.amount or (item.payos_payment_link_id and payment_link_id != item.payos_payment_link_id):
        return AdminPaymentResult(outcome="REJECTED", admin_payment_id=item.id, reason="Thông tin payOS không khớp.")
    if not reference:
        return AdminPaymentResult(outcome="REJECTED", admin_payment_id=item.id, reason="Thiếu reference payOS.")
    if await session.scalar(select(PaymentEvent.id).where(PaymentEvent.reference == reference)):
        return AdminPaymentResult(outcome="DUPLICATE", admin_payment_id=item.id, reference=reference)
    try:
        session.add(PaymentEvent(provider="PAYOS", reference=reference, order_code=order_code, amount=amount,
                                 raw_data=json.dumps(raw_data, ensure_ascii=False, sort_keys=True, default=str)))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return AdminPaymentResult(outcome="DUPLICATE", admin_payment_id=item.id, reference=reference)
    item.status = AdminPaymentStatus.PAID
    item.payos_reference = reference
    item.paid_at = datetime.now()
    admin = await session.get(User, item.payer_user_id or item.admin_user_id)
    receiver = await session.get(User, item.receiver_user_id) if item.receiver_user_id else None
    summary = await daily_settlement(session, admin, receiver, item.settlement_date or vietnam_today()) if admin and receiver else None
    outstanding = summary.remaining if summary else 0
    await session.commit()
    return AdminPaymentResult(outcome="PAID", admin_payment_id=item.id,
                              admin_telegram_id=admin.telegram_id if admin else None,
                              admin_name=admin.display_name if admin else None,
                              receiver_telegram_id=receiver.telegram_id if receiver else None,
                              outstanding=outstanding,
                              amount=amount, reference=reference)
