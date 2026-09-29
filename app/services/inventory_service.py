from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Inventory, Product, StockTransaction, TransactionType


async def import_products(session: AsyncSession, user_id: int, items: list[tuple[int, int]]) -> list[Inventory]:
    results: list[Inventory] = []
    try:
        seen: set[int] = set()
        for product_id, quantity in items:
            if product_id in seen:
                raise ValueError("Danh sách có sản phẩm bị trùng.")
            seen.add(product_id)
            if quantity <= 0:
                raise ValueError("Số lượng nhận phải lớn hơn 0.")
            product = await session.get(Product, product_id)
            if product is None or not product.active:
                raise ValueError("Sản phẩm không tồn tại hoặc đã bị ẩn.")
            inventory = await session.scalar(select(Inventory).where(
                Inventory.user_id == user_id, Inventory.product_id == product.id
            ))
            if inventory is None:
                inventory = Inventory(user_id=user_id, product_id=product.id, current_quantity=0)
                session.add(inventory)
            inventory.current_quantity += quantity
            session.add(StockTransaction(
                user_id=user_id, product_id=product.id,
                transaction_type=TransactionType.IMPORT, quantity=quantity,
                unit_price=product.price, commission_type=product.commission_type,
                commission_value=product.commission_value,
                product_name_snapshot=product.name,
                product_variant_snapshot=product.variant,
                product_unit_snapshot=product.unit,
                product_stock_unit_snapshot=product.stock_unit or "",
            ))
            results.append(inventory)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return results


async def list_inventory(session: AsyncSession, user_id: int, *, include_zero: bool = False) -> list[Inventory]:
    query = (
        select(Inventory)
        .options(joinedload(Inventory.product))
        .where(Inventory.user_id == user_id)
        .order_by(Inventory.id)
    )
    if not include_zero:
        query = query.where(Inventory.current_quantity > 0)
    return list((await session.scalars(query)).unique().all())
