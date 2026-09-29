from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyShippingFee


VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def vietnam_today() -> date:
    return datetime.now(VIETNAM_TZ).date()


async def get_shipping_fee(session: AsyncSession, user_id: int, business_date: date) -> int:
    amount = await session.scalar(select(DailyShippingFee.amount).where(
        DailyShippingFee.user_id == user_id,
        DailyShippingFee.business_date == business_date,
    ))
    return int(amount or 0)


async def set_shipping_fee(
    session: AsyncSession, user_id: int, business_date: date, amount: int
) -> DailyShippingFee:
    if amount < 0:
        raise ValueError("Tiền ship không được là số âm.")
    item = await session.scalar(select(DailyShippingFee).where(
        DailyShippingFee.user_id == user_id,
        DailyShippingFee.business_date == business_date,
    ))
    if item is None:
        item = DailyShippingFee(user_id=user_id, business_date=business_date, amount=amount)
        session.add(item)
    else:
        item.amount = amount
        item.updated_at = datetime.now()
    await session.commit()
    return item


async def get_shipping_total(
    session: AsyncSession,
    user_id: int,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    query = select(func.coalesce(func.sum(DailyShippingFee.amount), 0)).where(
        DailyShippingFee.user_id == user_id
    )
    if start_date is not None:
        query = query.where(DailyShippingFee.business_date >= start_date)
    if end_date is not None:
        query = query.where(DailyShippingFee.business_date <= end_date)
    return int(await session.scalar(query) or 0)


async def get_shipping_fees_by_date(
    session: AsyncSession, user_id: int, start_date: date, end_date: date
) -> dict[date, int]:
    rows = (await session.execute(select(
        DailyShippingFee.business_date, DailyShippingFee.amount,
    ).where(
        DailyShippingFee.user_id == user_id,
        DailyShippingFee.business_date >= start_date,
        DailyShippingFee.business_date <= end_date,
    ))).all()
    return {business_date: int(amount) for business_date, amount in rows}
