from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailySale, Product, StockTransaction, TransactionType
from app.services.shipping_service import get_shipping_total, vietnam_today


class Period(str, Enum):
    TODAY = "today"
    SEVEN_DAYS = "7days"
    MONTH = "month"
    ALL = "all"


@dataclass(frozen=True)
class Totals:
    revenue: int
    commission: int
    shipping: int
    company: int


def period_start(period: Period) -> date | None:
    today = vietnam_today()
    if period == Period.TODAY:
        return today
    if period == Period.SEVEN_DAYS:
        return today - timedelta(days=6)
    if period == Period.MONTH:
        return today.replace(day=1)
    return None


async def sales_totals(session: AsyncSession, user_id: int, period: Period) -> Totals:
    query = select(
        func.coalesce(func.sum(DailySale.revenue), 0),
        func.coalesce(func.sum(DailySale.commission_amount), 0),
        func.coalesce(func.sum(DailySale.company_amount), 0),
    ).where(DailySale.user_id == user_id)
    start = period_start(period)
    if start:
        query = query.where(DailySale.sale_date >= start)
    row = (await session.execute(query)).one()
    revenue, commission, company = (int(value) for value in row)
    shipping = await get_shipping_total(session, user_id, start, vietnam_today())
    return Totals(revenue, commission, shipping, max(0, company - shipping))


async def transaction_history(session: AsyncSession, user_id: int, period: Period):
    query = (
        select(StockTransaction, Product)
        .join(Product, Product.id == StockTransaction.product_id)
        .where(StockTransaction.user_id == user_id)
        .order_by(StockTransaction.created_at.desc())
        .limit(100)
    )
    start = period_start(period)
    if start:
        query = query.where(StockTransaction.created_at >= datetime.combine(start, time.min))
    return list((await session.execute(query)).all())
