import asyncio
from datetime import date
from pathlib import Path
import tempfile

from sqlalchemy import inspect, select

from app.database import create_database, initialize_database
from app.handlers.employee_history import render_day_pages
from app.models import CommissionType, DailySale, Product, StockTransaction, User
from app.services.employee_history_service import get_employee_day_history
from app.services.inventory_service import import_products, list_inventory
from app.services.sales_service import record_sale


def make_product(name: str, variant: str, package_unit: str, stock_unit: str | None) -> Product:
    return Product(
        name=name, variant=variant, unit=package_unit, stock_unit=stock_unit,
        price=50_000, commission_type=CommissionType.FIXED_PER_ITEM, commission_value=5_000,
    )


def test_stock_unit_is_separate_from_package_spec_and_snapshotted() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as folder:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(folder) / 'units.db').as_posix()}")
            await initialize_database(engine)
            async with engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync: {column["name"] for column in inspect(sync).get_columns("products")}
                )
                assert "stock_unit" in columns
            async with factory() as session:
                user = User(telegram_id=101, first_name="Nhân viên")
                fish_sauce = make_product("Nước mắm", "500", "ml", "chai")
                chili = make_product("Tương ớt", "250", "g", "chai")
                milk = make_product("Sữa", "180", "ml", "hộp")
                rice = make_product("Gạo", "", "", "kg")
                session.add_all([user, fish_sauce, chili, milk, rice]); await session.commit()

                assert fish_sauce.display_name == "Nước mắm 500ml"
                assert fish_sauce.quantity_unit == "chai"
                assert chili.quantity_unit == "chai"
                assert milk.quantity_unit == "hộp"
                assert rice.display_name == "Gạo" and rice.quantity_unit == "kg"

                await import_products(session, user.id, [(fish_sauce.id, 10)])
                inventory = (await list_inventory(session, user.id))[0]
                result = await record_sale(session, user.id, inventory.id, 3)
                assert result.remaining == 7 and result.stock_unit == "chai"

                transactions = list((await session.scalars(
                    select(StockTransaction).order_by(StockTransaction.id)
                )).all())
                sale = await session.scalar(select(DailySale))
                assert [row.quantity for row in transactions] == [10, -3]
                assert all(row.product_stock_unit_snapshot == "chai" for row in transactions)
                assert sale.quantity_sold == 3 and sale.product_stock_unit_snapshot == "chai"

                history = await get_employee_day_history(session, user.id, date.today())
                product_history = next(item for item in history.products if item.product_id == fish_sauce.id)
                assert product_history.quantity_unit == "chai"
                assert (product_history.import_qty, product_history.sale_qty, product_history.closing_stock) == (10, 3, 7)
                report = "\n".join(render_day_pages(history))
                assert "Nước mắm 500ml" in report
                assert "Nhập: 10 chai" in report
                assert "Bán: 3 chai" in report
                assert "Tồn cuối: 7 chai" in report
                assert "5000ml" not in report and "1500ml" not in report
            await engine.dispose()
    asyncio.run(scenario())


def test_legacy_product_fallback_never_uses_package_unit() -> None:
    legacy = make_product("Sản phẩm cũ", "500", "ml", None)
    assert legacy.display_name == "Sản phẩm cũ 500ml"
    assert legacy.quantity_unit == "sản phẩm"
