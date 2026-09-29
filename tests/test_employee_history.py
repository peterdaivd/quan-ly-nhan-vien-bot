import asyncio
from datetime import date, datetime
from pathlib import Path
import tempfile

from app.database import create_database, initialize_database
from app.models import (
    CommissionType, DailySale, Product, StockTransaction, TransactionType, User, UserRole,
)
from app.services.employee_history_service import (
    can_view_employee_history, get_employee_day_history, get_employee_range_summary,
)


def _stock(user_id, product, kind, quantity, at):
    return StockTransaction(
        user_id=user_id, product_id=product.id, transaction_type=kind, quantity=quantity,
        unit_price=product.price, commission_type=product.commission_type,
        commission_value=product.commission_value, product_name_snapshot=product.name,
        product_variant_snapshot=product.variant, product_unit_snapshot=product.unit,
        created_at=at,
    )


def _sale(user_id, product, quantity, at, *, price=None, commission_each=5_000):
    unit_price = product.price if price is None else price
    return DailySale(
        user_id=user_id, product_id=product.id, sale_date=at.date(), quantity_sold=quantity,
        revenue=unit_price * quantity, commission_amount=commission_each * quantity,
        company_amount=(unit_price - commission_each) * quantity, unit_price=unit_price,
        commission_type=CommissionType.FIXED_PER_ITEM, commission_value=commission_each,
        product_name_snapshot=product.name, product_variant_snapshot=product.variant,
        product_unit_snapshot=product.unit, created_at=at,
    )


def test_employee_history_day_range_permissions_and_snapshots() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as folder:
            url = f"sqlite+aiosqlite:///{(Path(folder) / 'history.db').as_posix()}"
            engine, factory = create_database(url)
            await initialize_database(engine)
            async with factory() as session:
                owner = User(telegram_id=1, first_name="Owner", role=UserRole.SUPER_ADMIN)
                admin_c = User(telegram_id=2, first_name="Admin C", role=UserRole.ADMIN)
                admin_d = User(telegram_id=3, first_name="Admin D", role=UserRole.ADMIN)
                user_a = User(telegram_id=4, first_name="A")
                c1 = User(telegram_id=5, first_name="C1")
                c2 = User(telegram_id=6, first_name="C2")
                session.add_all([owner, admin_c, admin_d, user_a, c1, c2]); await session.flush()
                c1.manager_admin_id = admin_c.id
                c2.manager_admin_id = admin_c.id
                user_a.manager_admin_id = admin_d.id
                melon = Product(name="Melon", unit="SP", price=50_000,
                                commission_type=CommissionType.FIXED_PER_ITEM, commission_value=5_000)
                coca = Product(name="Coca", variant="330", unit="chai", price=10_000,
                               commission_type=CommissionType.FIXED_PER_ITEM, commission_value=1_000)
                session.add_all([melon, coca]); await session.flush()

                # Tồn trước 29/09: Melon 5, Coca 10.
                session.add_all([
                    _stock(c1.id, melon, TransactionType.IMPORT, 5, datetime(2026, 9, 24, 8)),
                    _stock(c1.id, coca, TransactionType.IMPORT, 10, datetime(2026, 9, 24, 8)),
                ])
                # Dữ liệu kiểm thử một ngày; SALE được lưu ở cả hai bảng như production.
                for product, kind, quantity, at in [
                    (melon, TransactionType.IMPORT, 10, datetime(2026, 9, 29, 8, 15)),
                    (coca, TransactionType.IMPORT, 20, datetime(2026, 9, 29, 9, 20)),
                    (melon, TransactionType.SALE, -4, datetime(2026, 9, 29, 10, 25)),
                    (melon, TransactionType.IMPORT, 5, datetime(2026, 9, 29, 14, 30)),
                    (melon, TransactionType.SALE, -6, datetime(2026, 9, 29, 17, 10)),
                    (coca, TransactionType.SALE, -12, datetime(2026, 9, 29, 18, 0)),
                ]:
                    session.add(_stock(c1.id, product, kind, quantity, at))
                session.add_all([
                    _sale(c1.id, melon, 4, datetime(2026, 9, 29, 10, 25)),
                    _sale(c1.id, melon, 6, datetime(2026, 9, 29, 17, 10)),
                    _sale(c1.id, coca, 12, datetime(2026, 9, 29, 18, 0), price=10_000, commission_each=1_000),
                ])

                # Các ngày cho báo cáo khoảng.
                for day, imported, sold in [(25, 10, 5), (26, 20, 15), (27, 0, 8), (28, 5, 4)]:
                    if imported:
                        session.add(_stock(c2.id, melon, TransactionType.IMPORT, imported, datetime(2026, 9, day, 8)))
                    session.add(_stock(c2.id, melon, TransactionType.SALE, -sold, datetime(2026, 9, day, 12)))
                    session.add(_sale(c2.id, melon, sold, datetime(2026, 9, day, 12)))
                session.add_all([
                    _stock(c2.id, melon, TransactionType.IMPORT, 35, datetime(2026, 9, 29, 8)),
                    _stock(c2.id, melon, TransactionType.SALE, -22, datetime(2026, 9, 29, 12)),
                    _sale(c2.id, melon, 22, datetime(2026, 9, 29, 12)),
                ])
                await session.commit()

                history = await get_employee_day_history(session, c1.id, date(2026, 9, 29))
                by_id = {item.product_id: item for item in history.products}
                assert (by_id[melon.id].import_qty, by_id[melon.id].sale_qty) == (15, 10)
                assert (by_id[coca.id].import_qty, by_id[coca.id].sale_qty) == (20, 12)
                assert (by_id[melon.id].opening_stock, by_id[melon.id].closing_stock) == (5, 10)
                assert (by_id[coca.id].opening_stock, by_id[coca.id].closing_stock) == (10, 18)
                assert [event.created_at.strftime("%H:%M") for event in by_id[melon.id].events] == ["08:15", "10:25", "14:30", "17:10"]
                # Không double count SALE dù stock_transactions cũng có cùng lượt bán.
                assert by_id[melon.id].sale_qty == 10

                # Snapshot cũ không đổi khi catalog hiện tại thay đổi.
                melon.price, melon.commission_value = 60_000, 7_000
                await session.commit()
                same_history = await get_employee_day_history(session, c1.id, date(2026, 9, 29))
                same_melon = {item.product_id: item for item in same_history.products}[melon.id]
                assert same_melon.revenue == 500_000
                assert same_melon.commission == 50_000

                _, summaries = await get_employee_range_summary(session, c2.id, date(2026, 9, 25), date(2026, 9, 29))
                assert [(item.day.day, item.import_qty, item.sale_qty) for item in summaries] == [
                    (25, 10, 5), (26, 20, 15), (27, 0, 8), (28, 5, 4), (29, 35, 22),
                ]
                empty = await get_employee_day_history(session, user_a.id, date(2026, 9, 29))
                assert empty.products == [] and empty.total_revenue == 0

                assert can_view_employee_history(owner, user_a)
                assert can_view_employee_history(owner, admin_c)
                assert can_view_employee_history(owner, c1)
                assert can_view_employee_history(admin_c, c1)
                assert can_view_employee_history(admin_c, c2)
                assert not can_view_employee_history(admin_c, user_a)
                assert not can_view_employee_history(c1, c2)
            await engine.dispose()
    asyncio.run(scenario())
