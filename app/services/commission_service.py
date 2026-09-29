from decimal import Decimal

from app.models import CommissionType, Product
from app.models import UserProductCommission
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.utils.money import round_vnd


def calculate_commission(product: Product, quantity_sold: int, revenue: int) -> int:
    if quantity_sold < 0 or revenue < 0:
        raise ValueError("Số lượng và doanh thu không được âm.")
    if product.commission_type == CommissionType.PERCENT:
        return round_vnd(Decimal(revenue) * Decimal(product.commission_value) / Decimal(10_000))
    return product.commission_value * quantity_sold


def calculate_commission_values(commission_type: CommissionType, commission_value: int, quantity: int, revenue: int) -> int:
    if commission_type == CommissionType.PERCENT:
        return round_vnd(Decimal(revenue) * Decimal(commission_value) / Decimal(10_000))
    return commission_value * quantity


async def calculate_effective_commission(session: AsyncSession, user_id: int, product: Product, quantity: int, revenue: int) -> int:
    custom = await session.scalar(select(UserProductCommission).where(
        UserProductCommission.user_id == user_id,
        UserProductCommission.product_id == product.id,
    ))
    return calculate_commission_values(custom.commission_type if custom else product.commission_type,
                                       custom.commission_value if custom else product.commission_value,
                                       quantity, revenue)

