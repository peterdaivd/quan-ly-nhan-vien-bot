from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailySale, Product, StockTransaction, TransactionType, User, UserRole


VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_HISTORY_RANGE_DAYS = 90


@dataclass(frozen=True)
class HistoryEvent:
    event_type: str
    quantity: int
    created_at: datetime


@dataclass
class ProductHistory:
    product_id: int
    name: str
    variant: str
    unit: str
    opening_stock: int = 0
    import_qty: int = 0
    sale_qty: int = 0
    adjustment_qty: int = 0
    closing_stock: int = 0
    revenue: int = 0
    commission: int = 0
    events: list[HistoryEvent] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if not self.variant:
            size = self.unit.strip()
        elif not self.unit:
            size = self.variant.strip()
        else:
            compact_units = {"ml", "cl", "dl", "l", "mg", "g", "kg"}
            separator = "" if self.unit.casefold() in compact_units else " "
            size = f"{self.variant}{separator}{self.unit}".strip()
        return f"{self.name} {size}".strip()

    @property
    def quantity_unit(self) -> str:
        # variant thường là quy cách (330ml), còn unit rỗng trong catalog cũ nghĩa là SP.
        return self.unit.strip() or "SP"


@dataclass
class EmployeeHistory:
    user: User
    start_date: date
    end_date: date
    products: list[ProductHistory]
    total_revenue: int
    total_commission: int


@dataclass(frozen=True)
class DailyHistorySummary:
    day: date
    imports: tuple[tuple[str, str, int], ...]
    sales: tuple[tuple[str, str, int], ...]
    import_qty: int
    sale_qty: int
    revenue: int
    commission: int


def can_view_employee_history(actor: User | None, target: User | None) -> bool:
    if actor is None or target is None or actor.is_deleted:
        return False
    if actor.role == UserRole.SUPER_ADMIN:
        return True
    return (
        actor.role == UserRole.ADMIN
        and target.role == UserRole.USER
        and target.manager_admin_id == actor.id
    )


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return naïve local bounds matching this project's existing datetime.now storage."""
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def as_vietnam_time(value: datetime) -> datetime:
    # Existing rows are naïve local timestamps (models write datetime.now). Aware rows,
    # if supplied by another supported database, are converted exactly once.
    if value.tzinfo is None:
        return value.replace(tzinfo=VIETNAM_TZ)
    return value.astimezone(VIETNAM_TZ)


def _snapshot(transaction) -> tuple[str, str, str]:
    return (
        transaction.product_name_snapshot or f"Sản phẩm #{transaction.product_id}",
        transaction.product_variant_snapshot or "",
        transaction.product_unit_snapshot or "",
    )


async def get_employee_history(
    session: AsyncSession,
    user_id: int,
    start_datetime: datetime,
    end_datetime: datetime,
) -> EmployeeHistory:
    """Read historical stock and sales using set-based queries (no per-product query)."""
    if start_datetime >= end_datetime:
        raise ValueError("Khoảng thời gian không hợp lệ.")
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError("Nhân viên không tồn tại.")

    opening_rows = (await session.execute(
        select(
            StockTransaction.product_id,
            func.coalesce(func.sum(StockTransaction.quantity), 0),
            Product.name,
            Product.variant,
            Product.unit,
        )
        .join(Product, Product.id == StockTransaction.product_id)
        .where(
            StockTransaction.user_id == user_id,
            StockTransaction.created_at < start_datetime,
        )
        .group_by(StockTransaction.product_id, Product.name, Product.variant, Product.unit)
    )).all()
    stock_rows = list((await session.scalars(
        select(StockTransaction)
        .where(
            StockTransaction.user_id == user_id,
            StockTransaction.created_at >= start_datetime,
            StockTransaction.created_at < end_datetime,
        )
        .order_by(StockTransaction.created_at, StockTransaction.id)
    )).all())
    # daily_sales is authoritative for sold quantity and financial snapshots. The
    # matching stock_transactions SALE row is deliberately not used as a sale event.
    sale_rows = list((await session.scalars(
        select(DailySale)
        .where(
            DailySale.user_id == user_id,
            DailySale.created_at >= start_datetime,
            DailySale.created_at < end_datetime,
        )
        .order_by(DailySale.created_at, DailySale.id)
    )).all())

    products: dict[int, ProductHistory] = {
        int(product_id): ProductHistory(
            product_id=int(product_id),
            name=name,
            variant=variant,
            unit=unit,
            opening_stock=int(quantity),
        )
        for product_id, quantity, name, variant, unit in opening_rows
    }

    def product_for(row) -> ProductHistory:
        name, variant, unit = _snapshot(row)
        item = products.get(row.product_id)
        if item is None:
            item = ProductHistory(row.product_id, name, variant, unit)
            products[row.product_id] = item
        elif row.product_name_snapshot:
            item.name, item.variant, item.unit = name, variant, unit
        return item

    for row in stock_rows:
        item = product_for(row)
        quantity = int(row.quantity)
        if row.transaction_type == TransactionType.IMPORT:
            item.import_qty += quantity
            item.events.append(HistoryEvent("IMPORT", quantity, as_vietnam_time(row.created_at)))
        elif row.transaction_type == TransactionType.ADJUSTMENT:
            item.adjustment_qty += quantity
            item.events.append(HistoryEvent("ADJUSTMENT", quantity, as_vietnam_time(row.created_at)))

    for row in sale_rows:
        item = product_for(row)
        quantity = int(row.quantity_sold)
        item.sale_qty += quantity
        item.revenue += int(row.revenue)
        item.commission += int(row.commission_amount)
        item.events.append(HistoryEvent("SALE", -quantity, as_vietnam_time(row.created_at)))

    stock_delta: dict[int, int] = {}
    for row in stock_rows:
        stock_delta[row.product_id] = stock_delta.get(row.product_id, 0) + int(row.quantity)
    for item in products.values():
        item.closing_stock = item.opening_stock + stock_delta.get(item.product_id, 0)
        item.events.sort(key=lambda event: event.created_at)

    ordered = sorted(products.values(), key=lambda item: (item.name.casefold(), item.product_id))
    return EmployeeHistory(
        user=user,
        start_date=as_vietnam_time(start_datetime).date(),
        end_date=(as_vietnam_time(end_datetime) - timedelta(microseconds=1)).date(),
        products=ordered,
        total_revenue=sum(item.revenue for item in ordered),
        total_commission=sum(item.commission for item in ordered),
    )


async def get_employee_day_history(
    session: AsyncSession, user_id: int, day: date
) -> EmployeeHistory:
    start, end = local_day_bounds(day)
    return await get_employee_history(session, user_id, start, end)


async def get_employee_range_summary(
    session: AsyncSession, user_id: int, from_date: date, to_date: date
) -> tuple[EmployeeHistory, list[DailyHistorySummary]]:
    inclusive_days = (to_date - from_date).days + 1
    if inclusive_days <= 0:
        raise ValueError("Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.")
    if inclusive_days > MAX_HISTORY_RANGE_DAYS:
        raise ValueError(f"Khoảng thời gian tối đa là {MAX_HISTORY_RANGE_DAYS} ngày.")
    start, _ = local_day_bounds(from_date)
    _, end = local_day_bounds(to_date)
    history = await get_employee_history(session, user_id, start, end)
    buckets: dict[date, dict] = {
        from_date + timedelta(days=offset): {
            "imports": {}, "sales": {}, "import_qty": 0, "sale_qty": 0,
            "revenue": 0, "commission": 0,
        }
        for offset in range(inclusive_days)
    }
    for product in history.products:
        for event in product.events:
            day = event.created_at.date()
            if day not in buckets:
                continue
            if event.event_type == "IMPORT":
                buckets[day]["import_qty"] += event.quantity
                key = (product.display_name, product.quantity_unit)
                buckets[day]["imports"][key] = buckets[day]["imports"].get(key, 0) + event.quantity
            elif event.event_type == "SALE":
                buckets[day]["sale_qty"] += -event.quantity
                key = (product.display_name, product.quantity_unit)
                buckets[day]["sales"][key] = buckets[day]["sales"].get(key, 0) - event.quantity

    # Financial values are attached to products above; query once grouped by local
    # stored date so a range report preserves each historical sale snapshot.
    sales = list((await session.scalars(
        select(DailySale).where(
            DailySale.user_id == user_id,
            DailySale.created_at >= start,
            DailySale.created_at < end,
        )
    )).all())
    for sale in sales:
        day = as_vietnam_time(sale.created_at).date()
        if day in buckets:
            buckets[day]["revenue"] += int(sale.revenue)
            buckets[day]["commission"] += int(sale.commission_amount)

    summaries = [
        DailyHistorySummary(
            day=day,
            imports=tuple(
                (name, unit, quantity)
                for (name, unit), quantity in sorted(values["imports"].items())
            ),
            sales=tuple(
                (name, unit, quantity)
                for (name, unit), quantity in sorted(values["sales"].items())
            ),
            import_qty=values["import_qty"],
            sale_qty=values["sale_qty"],
            revenue=values["revenue"],
            commission=values["commission"],
        )
        for day, values in buckets.items()
    ]
    return history, summaries
