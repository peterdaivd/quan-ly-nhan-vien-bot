from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import DailySale, Inventory, StockTransaction, TransactionType, User, UserProductCommission
from app.services.commission_service import calculate_commission_values


@dataclass(frozen=True)
class SaleResult:
    product_name: str
    display_size: str
    stock_unit: str
    received_total: int
    quantity_sold: int
    remaining: int
    price: int
    revenue: int
    commission: int
    company_amount: int


async def record_sales(session: AsyncSession, user_id: int, items: list[tuple[int, int]]) -> list[SaleResult]:
    """Ghi cả phiếu bán trong một transaction; lỗi một dòng thì không trừ dòng nào."""
    if not items:
        raise ValueError("Phiếu bán đang trống.")
    if len({inventory_id for inventory_id, _ in items}) != len(items):
        raise ValueError("Phiếu bán có sản phẩm bị trùng.")
    if any(quantity <= 0 for _, quantity in items):
        raise ValueError("Số lượng bán phải lớn hơn 0.")
    results: list[SaleResult] = []
    try:
        seller = await session.get(User, user_id)
        if seller is None:
            raise PermissionError("Không tìm thấy người bán.")
        for inventory_id, quantity_sold in items:
            # user_id là lớp bảo vệ ownership; không tin record id từ client.
            inventory = await session.scalar(
                select(Inventory).options(joinedload(Inventory.product))
                .where(Inventory.id == inventory_id, Inventory.user_id == user_id)
            )
            if inventory is None:
                raise PermissionError("Không tìm thấy sản phẩm thuộc tài khoản của bạn.")
            revenue = inventory.product.price * quantity_sold
            custom = await session.scalar(select(UserProductCommission).where(
                UserProductCommission.user_id == user_id,
                UserProductCommission.product_id == inventory.product_id,
            ))
            commission_type = custom.commission_type if custom else inventory.product.commission_type
            commission_value = custom.commission_value if custom else inventory.product.commission_value
            commission = calculate_commission_values(commission_type, commission_value, quantity_sold, revenue)
            company_amount = revenue - commission
            if company_amount < 0:
                raise ValueError("Hoa hồng vượt quá doanh thu; không thể chốt bán hàng.")
            remaining = await session.scalar(
                update(Inventory)
                .where(
                    Inventory.id == inventory_id,
                    Inventory.user_id == user_id,
                    Inventory.current_quantity >= quantity_sold,
                )
                .values(current_quantity=Inventory.current_quantity - quantity_sold)
                .returning(Inventory.current_quantity)
                .execution_options(synchronize_session=False)
            )
            if remaining is None:
                raise ValueError(f"❌ {inventory.product.display_name}: số lượng bán vượt tồn hiện tại.")
            inventory.current_quantity = remaining
            session.add(StockTransaction(
                user_id=user_id, product_id=inventory.product_id,
                transaction_type=TransactionType.SALE, quantity=-quantity_sold,
                unit_price=inventory.product.price,
                commission_type=commission_type, commission_value=commission_value,
                product_name_snapshot=inventory.product.name,
                product_variant_snapshot=inventory.product.variant,
                product_unit_snapshot=inventory.product.unit,
                product_stock_unit_snapshot=inventory.product.stock_unit or "",
            ))
            session.add(DailySale(
                user_id=user_id, seller_role=seller.role, product_id=inventory.product_id, sale_date=date.today(),
                quantity_sold=quantity_sold, revenue=revenue,
                commission_amount=commission, company_amount=company_amount,
                unit_price=inventory.product.price,
                commission_type=commission_type, commission_value=commission_value,
                product_name_snapshot=inventory.product.name,
                product_variant_snapshot=inventory.product.variant,
                product_unit_snapshot=inventory.product.unit,
                product_stock_unit_snapshot=inventory.product.stock_unit or "",
            ))
            results.append(SaleResult(
                product_name=inventory.product.name, display_size=inventory.product.display_size,
                stock_unit=inventory.product.quantity_unit,
                received_total=remaining + quantity_sold, quantity_sold=quantity_sold,
                remaining=remaining, price=inventory.product.price,
                revenue=revenue, commission=commission, company_amount=company_amount,
            ))
        await session.commit()
        return results
    except Exception:
        await session.rollback()
        raise


async def record_sale(session: AsyncSession, user_id: int, inventory_id: int, quantity_sold: int) -> SaleResult:
    return (await record_sales(session, user_id, [(inventory_id, quantity_sold)]))[0]
