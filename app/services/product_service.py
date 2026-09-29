from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailySale, Inventory, Product, StockTransaction


def split_size(value: str) -> tuple[str, str]:
    value = value.strip()
    if not value:
        raise ValueError("Quy cách không được để trống.")
    match = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*([^\d\s].*)", value)
    if match:
        return match.group(1).replace(",", "."), match.group(2).strip()
    return value, ""


async def active_products(session: AsyncSession) -> list[Product]:
    return list((await session.scalars(
        select(Product).where(Product.active.is_(True)).order_by(Product.name, Product.variant, Product.unit, Product.id)
    )).all())


async def all_products(session: AsyncSession) -> list[Product]:
    return list((await session.scalars(
        select(Product).order_by(Product.active.desc(), Product.name, Product.variant, Product.unit, Product.id)
    )).all())


async def product_has_history(session: AsyncSession, product_id: int) -> bool:
    for model in (Inventory, StockTransaction, DailySale):
        if await session.scalar(select(func.count(model.id)).where(model.product_id == product_id)):
            return True
    return False


async def delete_or_hide_product(session: AsyncSession, product: Product) -> bool:
    """Trả True nếu xóa cứng; False nếu chỉ ẩn do đã có lịch sử."""
    if await product_has_history(session, product.id):
        product.active = False
        await session.commit()
        return False
    await session.delete(product)
    await session.commit()
    return True
