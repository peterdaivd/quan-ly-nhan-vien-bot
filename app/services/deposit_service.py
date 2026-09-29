from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Deposit, DepositStatus, PaymentEvent, PaymentOrderSequence, User,
)
from app.services.wallet_service import WalletCreditResult, credit_paid_deposit


@dataclass(frozen=True)
class DepositProcessResult:
    outcome: str
    deposit_id: int | None = None
    telegram_id: int | None = None
    reference: str | None = None
    amount: int | None = None
    credit: WalletCreditResult | None = None
    reason: str | None = None


async def generate_order_code(session: AsyncSession) -> int:
    sequence = PaymentOrderSequence()
    session.add(sequence)
    await session.flush()
    # Sequence DB tăng đơn điệu đảm bảo duy nhất; tiền tố giữ orderCode dễ nhận diện.
    return 1_000_000_000_000 + sequence.id


async def create_deposit(
    session: AsyncSession, user_id: int, amount: int, expires_minutes: int
) -> Deposit:
    if amount < 1_000:
        raise ValueError("Số tiền nạp tối thiểu là 1.000đ.")
    if amount > 9_000_000_000_000:
        raise ValueError("Số tiền nạp vượt quá giới hạn hỗ trợ.")
    order_code = await generate_order_code(session)
    deposit = Deposit(
        user_id=user_id,
        order_code=order_code,
        amount=amount,
        status=DepositStatus.PENDING,
        expires_at=datetime.now() + timedelta(minutes=expires_minutes),
    )
    session.add(deposit)
    await session.commit()  # Lưu orderCode trước khi gọi API payOS.
    return deposit


async def mark_link_created(
    session: AsyncSession,
    deposit_id: int,
    *,
    payment_link_id: str,
    checkout_url: str,
    qr_code: str,
) -> Deposit:
    deposit = await session.get(Deposit, deposit_id)
    if deposit is None:
        raise ValueError("Không tìm thấy yêu cầu nạp.")
    deposit.payos_payment_link_id = payment_link_id
    deposit.checkout_url = checkout_url
    deposit.qr_code = qr_code
    await session.commit()
    return deposit


async def mark_deposit_failed(session: AsyncSession, deposit_id: int) -> None:
    await session.execute(
        update(Deposit)
        .where(Deposit.id == deposit_id, Deposit.status == DepositStatus.PENDING)
        .values(status=DepositStatus.FAILED)
    )
    await session.commit()


async def expire_pending_deposits(session: AsyncSession) -> int:
    result = await session.execute(
        update(Deposit)
        .where(Deposit.status == DepositStatus.PENDING, Deposit.expires_at <= datetime.now())
        .values(status=DepositStatus.EXPIRED)
    )
    await session.commit()
    return result.rowcount or 0


async def owned_deposit(session: AsyncSession, deposit_id: int, user_id: int) -> Deposit | None:
    deposit = await session.scalar(select(Deposit).where(
        Deposit.id == deposit_id, Deposit.user_id == user_id
    ))
    if deposit and deposit.status == DepositStatus.PENDING and deposit.expires_at <= datetime.now():
        deposit.status = DepositStatus.EXPIRED
        await session.commit()
    return deposit


async def mark_cancelled(session: AsyncSession, deposit_id: int, user_id: int) -> bool:
    result = await session.execute(
        update(Deposit)
        .where(
            Deposit.id == deposit_id,
            Deposit.user_id == user_id,
            Deposit.status == DepositStatus.PENDING,
        )
        .values(status=DepositStatus.CANCELLED)
    )
    await session.commit()
    return bool(result.rowcount)


async def process_successful_deposit(
    session: AsyncSession,
    *,
    order_code: int,
    amount: int,
    reference: str,
    payment_link_id: str,
    raw_data: dict[str, Any],
) -> DepositProcessResult:
    """Hàm idempotent dùng chung cho webhook và nút Kiểm tra."""
    if not reference:
        return DepositProcessResult(outcome="REJECTED", reason="Thiếu reference payOS.")
    if await session.scalar(select(PaymentEvent.id).where(PaymentEvent.reference == reference)):
        return DepositProcessResult(outcome="DUPLICATE", reference=reference)
    try:
        session.add(PaymentEvent(
            provider="PAYOS",
            reference=reference,
            order_code=order_code,
            amount=amount,
            raw_data=json.dumps(raw_data, ensure_ascii=False, sort_keys=True, default=str),
        ))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return DepositProcessResult(outcome="DUPLICATE", reference=reference)

    deposit = await session.scalar(select(Deposit).where(Deposit.order_code == order_code))
    if deposit is None:
        await session.commit()
        return DepositProcessResult(outcome="REJECTED", reference=reference, reason="Không tìm thấy orderCode.")
    if deposit.status != DepositStatus.PENDING:
        await session.commit()
        return DepositProcessResult(
            outcome="DUPLICATE" if deposit.status == DepositStatus.PAID else "REJECTED",
            deposit_id=deposit.id,
            reference=reference,
            reason=f"Deposit đang ở trạng thái {deposit.status.value}.",
        )
    if amount != deposit.amount:
        deposit.status = DepositStatus.REVIEW
        await session.commit()
        return DepositProcessResult(
            outcome="REVIEW", deposit_id=deposit.id, reference=reference,
            reason="Số tiền payOS không khớp deposit.",
        )
    if deposit.payos_payment_link_id and payment_link_id != deposit.payos_payment_link_id:
        deposit.status = DepositStatus.REVIEW
        await session.commit()
        return DepositProcessResult(
            outcome="REVIEW", deposit_id=deposit.id, reference=reference,
            reason="paymentLinkId không khớp.",
        )

    claimed = await session.scalar(
        update(Deposit)
        .where(Deposit.id == deposit.id, Deposit.status == DepositStatus.PENDING)
        .values(
            status=DepositStatus.PAID,
            paid_at=datetime.now(),
            payos_reference=reference,
        )
        .returning(Deposit.id)
        .execution_options(synchronize_session=False)
    )
    if claimed is None:
        await session.commit()
        return DepositProcessResult(outcome="DUPLICATE", deposit_id=deposit.id, reference=reference)

    credit = await credit_paid_deposit(session, deposit, reference)
    deposit.status = DepositStatus.PAID
    deposit.paid_at = datetime.now()
    deposit.payos_reference = reference
    user = await session.get(User, deposit.user_id)
    await session.commit()
    return DepositProcessResult(
        outcome="PAID",
        deposit_id=deposit.id,
        telegram_id=user.telegram_id if user else None,
        reference=reference,
        amount=amount,
        credit=credit,
    )
