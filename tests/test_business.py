import asyncio
from pathlib import Path
import tempfile

from sqlalchemy import func, select

from app.database import create_database, initialize_database
from app.handlers.admin import product_management_keyboard, product_page_keyboard
from app.keyboards.inventory import PAGE_SIZE, product_grid, quantity_keyboard
from app.models import CommissionType, DailySale, Product, StockTransaction, User, UserProductCommission
from app.services.inventory_service import import_products, list_inventory
from app.services.product_service import delete_or_hide_product
from app.services.sales_service import record_sale, record_sales


def make_product(**overrides) -> Product:
    values = dict(name="Coca Cola", variant="330", unit="ml", price=12_000,
                  commission_type=CommissionType.PERCENT, commission_value=1000, active=True)
    values.update(overrides)
    return Product(**values)


def test_catalog_import_sale_isolation_and_snapshots() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(temp_dir) / 'business.db').as_posix()}")
            await initialize_database(engine)
            async with factory() as session:
                owner, other, product = User(telegram_id=1, first_name="A"), User(telegram_id=2, first_name="B"), make_product()
                session.add_all([owner, other, product]); await session.commit()
                await import_products(session, owner.id, [(product.id, 10)])
                assert (await list_inventory(session, owner.id))[0].current_quantity == 10
                assert await list_inventory(session, other.id) == []
                inventory = (await list_inventory(session, owner.id))[0]
                owner_id, other_id, inventory_id = owner.id, other.id, inventory.id
                product_id = product.id
                try:
                    await record_sale(session, other_id, inventory_id, 1)
                    raise AssertionError("Không được bán tồn của người khác")
                except PermissionError:
                    pass
                result = await record_sale(session, owner_id, inventory_id, 8)
                assert (result.remaining, result.revenue, result.commission, result.company_amount) == (2, 96_000, 9_600, 86_400)
                sale = await session.scalar(select(DailySale))
                assert (sale.unit_price, sale.commission_type, sale.commission_value) == (12_000, CommissionType.PERCENT, 1000)
                assert (sale.product_name_snapshot, sale.product_variant_snapshot, sale.product_unit_snapshot) == ("Coca Cola", "330", "ml")
                product = await session.get(Product, product_id)
                product.name, product.variant = "Coca Cola Plus", "500"
                product.price, product.commission_value = 15_000, 1200
                await session.commit()
                assert (sale.unit_price, sale.commission_value, sale.revenue) == (12_000, 1000, 96_000)
                await import_products(session, owner_id, [(product_id, 5)])
                assert (await list_inventory(session, owner_id))[0].current_quantity == 7
                updated_inventory = (await list_inventory(session, owner_id))[0]
                assert updated_inventory.product_id == product_id
                new_sale = await record_sale(session, owner_id, updated_inventory.id, 1)
                assert (new_sale.price, new_sale.revenue, new_sale.commission) == (15_000, 15_000, 1_800)
                sales = list((await session.scalars(select(DailySale).order_by(DailySale.id))).all())
                assert [(row.unit_price, row.commission_value) for row in sales] == [(12_000, 1000), (15_000, 1200)]
                assert sales[0].product_name_snapshot == "Coca Cola"
                assert sales[1].product_name_snapshot == "Coca Cola Plus"
                rows = list((await session.scalars(select(StockTransaction).order_by(StockTransaction.id))).all())
                assert [row.unit_price for row in rows] == [12_000, 12_000, 15_000, 15_000]
                assert await delete_or_hide_product(session, product) is False
                assert product.active is False
            await engine.dispose()
    asyncio.run(scenario())


def test_fixed_commission_and_stock_guards() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(temp_dir) / 'fixed.db').as_posix()}")
            await initialize_database(engine)
            async with factory() as session:
                user = User(telegram_id=1, first_name="A", role="ADMIN")
                product = make_product(name="Pepsi", price=11_000, commission_type=CommissionType.FIXED_PER_ITEM, commission_value=1000)
                session.add_all([user, product]); await session.commit()
                await import_products(session, user.id, [(product.id, 15)])
                inventory = (await list_inventory(session, user.id))[0]
                user_id, inventory_id = user.id, inventory.id
                product_id = product.id
                session.add(UserProductCommission(user_id=user_id, product_id=product_id, created_by_super_admin_id=user_id,
                                                      commission_type=CommissionType.PERCENT, commission_value=2000))
                await session.commit()
                try:
                    await record_sale(session, user_id, inventory_id, 16)
                    raise AssertionError("Không được bán vượt tồn")
                except ValueError:
                    pass
                result = await record_sale(session, user_id, inventory_id, 8)
                assert (result.revenue, result.commission, result.company_amount, result.remaining) == (88_000, 17_600, 70_400, 7)
                sale = await session.scalar(select(DailySale).where(DailySale.user_id == user_id))
                assert sale.seller_role.value == "ADMIN"
                try:
                    await import_products(session, user_id, [(product_id, 1), (product_id, 2)])
                    raise AssertionError("Không được nhập trùng sản phẩm trong một lần")
                except ValueError:
                    pass
                assert (await list_inventory(session, user_id))[0].current_quantity == 7
            await engine.dispose()
    asyncio.run(scenario())


def test_product_pagination_and_callback_lengths() -> None:
    labels = [f"🥤 Sản phẩm {index}" for index in range(9, 17)]
    keyboard = product_grid(labels, 1, 3)
    flattened = [button.text for row in keyboard.keyboard for button in row]
    assert all(label in flattened for label in labels)
    assert "📄 2/3" in flattened
    assert "◀️ Trang trước" in flattened and "Trang sau ▶️" in flattened
    quantities = [button.text for row in quantity_keyboard(1).keyboard for button in row]
    assert {"➖", "➕", "1️⃣", "2️⃣", "3️⃣", "5️⃣", "🔟", "✅ Đưa ra sản phẩm"}.issubset(quantities)
    admin_products = [make_product(id=index, name=f"P{index}") for index in range(1, 9)]
    admin_keyboard = product_page_keyboard(admin_products, "edit", 1, 3)
    admin_buttons = [button for row in admin_keyboard.inline_keyboard for button in row]
    assert len([button for button in admin_buttons if (button.callback_data or "").startswith("prod:select:")]) == 8
    assert all(len(button.callback_data or "") <= 64 for button in admin_buttons)
    menu_labels = {button.text for row in product_management_keyboard().inline_keyboard for button in row}
    assert {"➕ Thêm sản phẩm", "📋 Danh sách sản phẩm", "✏️ Sửa sản phẩm",
            "🚫 Ẩn sản phẩm", "✅ Hiện sản phẩm", "⬅️ Quay lại"}.issubset(menu_labels)


def test_hard_delete_only_without_history() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(temp_dir) / 'delete.db').as_posix()}")
            await initialize_database(engine)
            async with factory() as session:
                product = make_product(); session.add(product); await session.commit(); product_id = product.id
                assert await delete_or_hide_product(session, product) is True
                assert await session.get(Product, product_id) is None
                assert await session.scalar(select(func.count(Product.id))) == 0
            await engine.dispose()
    asyncio.run(scenario())


def test_multi_product_sale_is_atomic() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(temp_dir) / 'batch.db').as_posix()}")
            await initialize_database(engine)
            async with factory() as session:
                user = User(telegram_id=10, first_name="Batch")
                first = make_product(name="Coca")
                second = make_product(name="Melon", price=5_000, commission_type=CommissionType.FIXED_PER_ITEM, commission_value=500)
                session.add_all([user, first, second]); await session.commit()
                user_id, first_id, second_id = user.id, first.id, second.id
                await import_products(session, user_id, [(first_id, 10), (second_id, 5)])
                inventory = await list_inventory(session, user_id)
                first_inventory, second_inventory = inventory[0].id, inventory[1].id
                try:
                    await record_sales(session, user_id, [(first_inventory, 2), (second_inventory, 99)])
                    raise AssertionError("Phiếu lỗi phải rollback toàn bộ")
                except ValueError:
                    pass
                quantities = [item.current_quantity for item in await list_inventory(session, user_id)]
                assert quantities == [10, 5]
                results = await record_sales(session, user_id, [(first_inventory, 2), (second_inventory, 3)])
                assert [result.remaining for result in results] == [8, 2]
                assert sum(result.revenue for result in results) == 39_000
                assert await session.scalar(select(func.count(DailySale.id))) == 2
            await engine.dispose()
    asyncio.run(scenario())
