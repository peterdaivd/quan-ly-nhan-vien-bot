from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentAdjustment, PaymentAdjustmentType, User, UserRole


VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class DebtResetResult:
    adjustment: PaymentAdjustment | None
    amount: int
    created: bool


def can_reset_debt(actor: User | None, target: User | None) -> bool:
    if actor is None or target is None or actor.is_deleted or target.is_deleted:
        return False
    if target.role == UserRole.SUPER_ADMIN:
        return False
    if actor.role == UserRole.SUPER_ADMIN:
        return target.role in {UserRole.USER, UserRole.ADMIN}
    return (
        actor.role == UserRole.ADMIN
        and target.role == UserRole.USER
        and target.manager_admin_id == actor.id
    )


async def adjustment_total(
    session: AsyncSession,
    user_id: int,
    receiver_user_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    query = select(func.coalesce(func.sum(PaymentAdjustment.amount), 0)).where(
        PaymentAdjustment.user_id == user_id,
        PaymentAdjustment.receiver_user_id == receiver_user_id,
        PaymentAdjustment.adjustment_type == PaymentAdjustmentType.DEBT_RESET,
    )
    if start_date is not None:
        query = query.where(PaymentAdjustment.business_date >= start_date)
    if end_date is not None:
        query = query.where(PaymentAdjustment.business_date <= end_date)
    return int(await session.scalar(query) or 0)


async def reset_debt(
    session: AsyncSession,
    actor: User,
    target: User,
    *,
    note: str | None = None,
) -> DebtResetResult:
    if not can_reset_debt(actor, target):
        raise PermissionError("Bạn không có quyền reset công nợ của tài khoản này.")

    # Local imports avoid a service import cycle.
    from app.services.admin_payment_service import debt_summary_to_receiver, get_payment_receiver

    receiver = await get_payment_receiver(session, target)
    if receiver is None:
        raise ValueError("Không tìm thấy người nhận tiền của tài khoản này.")
    summary = await debt_summary_to_receiver(session, target, receiver)
    if summary.outstanding <= 0:
        return DebtResetResult(None, 0, False)

    state = (
        f"DEBT_RESET|{target.id}|{receiver.id}|{summary.gross_due}|"
        f"{summary.paid}|{summary.adjusted}"
    )
    key = hashlib.sha256(state.encode("utf-8")).hexdigest()
    now_vietnam = datetime.now(VIETNAM_TZ)
    item = PaymentAdjustment(
        user_id=target.id,
        receiver_user_id=receiver.id,
        amount=summary.outstanding,
        adjustment_type=PaymentAdjustmentType.DEBT_RESET,
        business_date=now_vietnam.date(),
        created_by_user_id=actor.id,
        idempotency_key=key,
        note=note or "Reset công nợ về 0",
        created_at=now_vietnam.replace(tzinfo=None),
    )
    session.add(item)
    try:
        await session.commit()
        return DebtResetResult(item, item.amount, True)
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(PaymentAdjustment).where(
            PaymentAdjustment.idempotency_key == key
        ))
        return DebtResetResult(existing, 0, False)


async def reset_history(
    session: AsyncSession, user_id: int, *, limit: int = 30
) -> list[PaymentAdjustment]:
    return list((await session.scalars(select(PaymentAdjustment).where(
        PaymentAdjustment.user_id == user_id,
        PaymentAdjustment.adjustment_type == PaymentAdjustmentType.DEBT_RESET,
    ).order_by(PaymentAdjustment.created_at.desc(), PaymentAdjustment.id.desc()).limit(limit))).all())
