from __future__ import annotations

from math import ceil

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.inventory import QUICK_AMOUNTS, button_map, commission_text, require_employee
from app.keyboards.inventory import (
    PAGE_SIZE, cart_keyboard, final_confirmation_keyboard, operation_menu,
    product_grid, quantity_keyboard,
)
from app.keyboards.user import user_menu
from app.services.commission_service import calculate_effective_commission
from app.services.inventory_service import list_inventory
from app.services.sales_service import record_sales
from app.services.statistics_service import Period, sales_totals
from app.utils.money import format_money
from app.utils.parser import parse_integer

router = Router(name="sales")


class SaleState(StatesGroup):
    menu = State()
    products = State()
    quantity = State()
    confirming = State()


async def show_sale_menu(message: Message, state: FSMContext, text: str = "📊 CHỐT BÁN HÀNG\n\nHãy chọn thao tác:") -> None:
    await state.set_state(SaleState.menu)
    await message.answer(text, reply_markup=operation_menu("bán"))


async def show_sale_products(message: Message, state: FSMContext, session: AsyncSession, user_id: int, page: int = 0) -> None:
    inventories = await list_inventory(session, user_id)
    if not inventories:
        await state.set_state(SaleState.menu)
        return await message.answer("Bạn chưa có sản phẩm nào còn tồn.", reply_markup=operation_menu("bán"))
    pages = max(1, ceil(len(inventories) / PAGE_SIZE)); page = max(0, min(page, pages - 1))
    current = inventories[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    mapping = button_map(current)
    await state.update_data(page=page, product_buttons=mapping)
    await state.set_state(SaleState.products)
    await message.answer(
        "📦 SẢN PHẨM ĐANG CÓ\n\nChọn sản phẩm đã bán:",
        reply_markup=product_grid(list(mapping), page, pages),
    )


def sale_quantity_text(inventory, quantity: int) -> str:
    return (
        "📦 SẢN PHẨM ĐANG CHỌN\n\n"
        f"Tên: {inventory.product.name}\nQuy cách: {inventory.product.display_size or 'Không có'}\n"
        f"Đơn vị quản lý: {inventory.product.quantity_unit}\n"
        f"Tồn hiện tại: {inventory.current_quantity} {inventory.product.quantity_unit}\nGiá: {format_money(inventory.product.price)}\n"
        f"Hoa hồng: {commission_text(inventory.product)}\n\nSố lượng đang chọn: {quantity} {inventory.product.quantity_unit}"
    )


async def get_owned_inventory(session: AsyncSession, user_id: int, inventory_id: int):
    return next((item for item in await list_inventory(session, user_id) if item.id == inventory_id), None)


async def select_sale_product(message: Message, state: FSMContext, session: AsyncSession, user_id: int, inventory_id: int) -> None:
    inventory = await get_owned_inventory(session, user_id, inventory_id)
    if inventory is None: return await message.answer("Sản phẩm không thuộc tài khoản hoặc đã hết hàng.")
    await state.set_state(SaleState.quantity)
    sent = await message.answer(sale_quantity_text(inventory, 1), reply_markup=quantity_keyboard(1))
    await state.update_data(current_id=inventory.id, current_quantity=1, quantity_message_id=sent.message_id)


async def refresh_sale_quantity(message: Message, state: FSMContext, session: AsyncSession, user_id: int, quantity: int) -> None:
    data = await state.get_data(); inventory = await get_owned_inventory(session, user_id, int(data.get("current_id", 0)))
    if inventory is None:
        await state.set_state(SaleState.menu)
        return await message.answer("Sản phẩm không thuộc tài khoản hoặc đã hết hàng.", reply_markup=operation_menu("bán"))
    if quantity > inventory.current_quantity:
        return await message.answer(f"❌ Chỉ còn {inventory.current_quantity} {inventory.product.quantity_unit} trong kho.")
    await state.update_data(current_quantity=quantity)
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=int(data["quantity_message_id"]))
    except (TelegramBadRequest, KeyError):
        pass
    sent = await message.answer(sale_quantity_text(inventory, quantity), reply_markup=quantity_keyboard(quantity))
    await state.update_data(quantity_message_id=sent.message_id)


async def sale_cart_text(session: AsyncSession, user_id: int, cart: dict[str, int], *, final: bool = False) -> str:
    inventories = {item.id: item for item in await list_inventory(session, user_id)}
    lines = ["📊 XÁC NHẬN BÁN HÀNG" if final else "📋 DANH SÁCH ĐANG BÁN"]
    total_revenue = total_commission = total_company = 0
    for index, (raw_id, raw_quantity) in enumerate(cart.items(), 1):
        inventory = inventories.get(int(raw_id)); quantity = int(raw_quantity)
        if inventory is None:
            continue
        revenue = inventory.product.price * quantity
        commission = await calculate_effective_commission(session, user_id, inventory.product, quantity, revenue)
        company = revenue - commission
        total_revenue += revenue
        total_commission += commission; total_company += company
        if final:
            lines.append(
                f"\n{index}. {inventory.product.display_name}\nTồn trước: {inventory.current_quantity} {inventory.product.quantity_unit}\n"
                f"Số lượng bán: {quantity} {inventory.product.quantity_unit}\n"
                f"Tồn sau: {inventory.current_quantity - quantity} {inventory.product.quantity_unit}\n"
                f"Giá bán: {format_money(inventory.product.price)}\nDoanh thu: {format_money(revenue)}\n"
                f"Hoa hồng: {format_money(commission)}\nTrả công ty: {format_money(company)}"
            )
        else:
            lines.append(f"\n{index}. {inventory.product.display_name} x {quantity} {inventory.product.quantity_unit}")
    lines.extend(["\n━━━━━━━━━━━━", f"Tổng số loại: {len(cart)}"])
    if final:
        lines.extend([
            f"\nTổng doanh thu: {format_money(total_revenue)}",
            f"Tổng hoa hồng: {format_money(total_commission)}",
            f"Tổng trả công ty: {format_money(total_company)}",
        ])
    return "\n".join(lines)


@router.message(F.text == "📊 Chốt bán hàng")
async def start_sale(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: return
    await state.set_data({"cart": {}, "user_id": user.id})
    await show_sale_menu(message, state)


@router.message(SaleState.menu, F.text == "📦 Sản phẩm")
async def sale_products_menu(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if user: await show_sale_products(message, state, session, user.id)


@router.message(SaleState.products, F.text == "◀️ Trang trước")
async def sale_previous_page(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if user: await show_sale_products(message, state, session, user.id, int((await state.get_data()).get("page", 0)) - 1)


@router.message(SaleState.products, F.text == "Trang sau ▶️")
async def sale_next_page(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if user: await show_sale_products(message, state, session, user.id, int((await state.get_data()).get("page", 0)) + 1)


@router.message(SaleState.products, F.text.startswith("📄 "))
async def sale_page_indicator(message: Message) -> None: await message.answer("Đây là số trang hiện tại.")


@router.message(SaleState.products, F.text == "📋 Xem lựa chọn")
async def view_sale_cart(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: return
    cart = (await state.get_data()).get("cart", {})
    if not cart: return await message.answer("❌ Bạn chưa chọn sản phẩm nào.")
    await state.set_state(SaleState.menu)
    await message.answer(await sale_cart_text(session, user.id, cart), reply_markup=cart_keyboard("bán"))


@router.message(SaleState.products, F.text == "⬅️ Quay lại")
async def sale_products_back(message: Message, state: FSMContext) -> None: await show_sale_menu(message, state)


@router.message(SaleState.products)
async def choose_sale_product(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: return
    inventory_id = (await state.get_data()).get("product_buttons", {}).get(message.text or "")
    if inventory_id is None: return await message.answer("Hãy chọn sản phẩm bằng nút trên bàn phím.")
    await select_sale_product(message, state, session, user.id, int(inventory_id))


@router.message(SaleState.quantity, F.text == "✅ Đưa ra sản phẩm")
async def add_sale_item(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: await state.clear(); return
    data = await state.get_data(); inventory = await get_owned_inventory(session, user.id, int(data.get("current_id", 0)))
    if inventory is None: return await message.answer("Sản phẩm không thuộc tài khoản hoặc đã hết hàng.")
    cart = dict(data.get("cart", {})); quantity = int(data.get("current_quantity", 1))
    combined = int(cart.get(str(inventory.id), 0)) + quantity
    if combined > inventory.current_quantity:
        return await message.answer(
            f"❌ Tổng đã chọn {combined} {inventory.product.quantity_unit}, "
            f"nhưng kho chỉ còn {inventory.current_quantity} {inventory.product.quantity_unit}."
        )
    cart[str(inventory.id)] = combined; await state.update_data(cart=cart)
    text = (
        f"✅ Đã thêm sản phẩm\n\n{inventory.product.display_name}\n"
        f"Số lượng thêm: {quantity} {inventory.product.quantity_unit}\n\n"
        + await sale_cart_text(session, user.id, cart)
    )
    await show_sale_menu(message, state, text)


@router.message(SaleState.quantity, F.text == "🗑 Xóa lựa chọn")
async def remove_current_sale_item(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: await state.clear(); return
    data = await state.get_data(); cart = dict(data.get("cart", {})); cart.pop(str(data.get("current_id", "")), None)
    await state.update_data(cart=cart); await message.answer("Đã bỏ sản phẩm đang chọn.")
    await show_sale_products(message, state, session, user.id, int(data.get("page", 0)))


@router.message(SaleState.quantity, F.text == "⬅️ Quay lại")
async def sale_quantity_back(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if user: await show_sale_products(message, state, session, user.id, int((await state.get_data()).get("page", 0)))


@router.message(SaleState.quantity)
async def change_sale_quantity(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: await state.clear(); return
    data = await state.get_data(); current = int(data.get("current_quantity", 1)); text = (message.text or "").strip()
    if text == "➖": quantity = max(1, current - 1)
    elif text == "➕": quantity = current + 1
    elif text in QUICK_AMOUNTS: quantity = current + QUICK_AMOUNTS[text]
    elif text.startswith("🔢 "): return
    else:
        try: quantity = parse_integer(text, "Số lượng")
        except ValueError as exc: return await message.answer(f"❌ {exc}")
    await refresh_sale_quantity(message, state, session, user.id, quantity)


@router.message(SaleState.menu, F.text == "🗑 Xóa lựa chọn")
async def clear_sale_cart(message: Message, state: FSMContext) -> None:
    await state.update_data(cart={}); await show_sale_menu(message, state, "🗑 Đã xóa toàn bộ lựa chọn bán hàng.")


@router.message(SaleState.menu, F.text == "⬅️ Quay lại")
async def sale_back_main(message: Message, state: FSMContext) -> None:
    await state.clear(); await message.answer("Menu chính", reply_markup=user_menu())


@router.message(SaleState.menu, F.text == "✅ Xác nhận bán")
async def preview_sale(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: return
    cart = (await state.get_data()).get("cart", {})
    if not cart: return await message.answer("❌ Bạn chưa chọn sản phẩm.")
    inventories = {item.id: item for item in await list_inventory(session, user.id)}
    for raw_id, raw_quantity in cart.items():
        item = inventories.get(int(raw_id))
        if item is None or int(raw_quantity) > item.current_quantity:
            return await message.answer("❌ Tồn kho đã thay đổi. Hãy kiểm tra lại phiếu bán.")
    await state.set_state(SaleState.confirming)
    await message.answer(await sale_cart_text(session, user.id, cart, final=True), reply_markup=final_confirmation_keyboard())


@router.message(SaleState.confirming, F.text == "✏️ Chọn thêm sản phẩm")
async def sale_choose_more(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if user: await show_sale_products(message, state, session, user.id)


@router.message(SaleState.confirming, F.text == "🗑 Xóa lựa chọn")
async def clear_confirming_sale(message: Message, state: FSMContext) -> None:
    await state.update_data(cart={}); await show_sale_menu(message, state, "🗑 Đã xóa toàn bộ lựa chọn bán hàng.")


@router.message(SaleState.confirming, F.text == "❌ Hủy")
async def cancel_sale(message: Message, state: FSMContext) -> None:
    await state.clear(); await message.answer("Đã hủy chốt bán hàng.", reply_markup=user_menu())


@router.message(SaleState.confirming, F.text == "✅ Xác nhận")
async def confirm_sale(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: await state.clear(); return
    cart = (await state.get_data()).get("cart", {}); items = [(int(key), int(value)) for key, value in cart.items()]
    try: results = await record_sales(session, user.id, items)
    except (ValueError, PermissionError) as exc:
        await session.rollback(); await state.set_state(SaleState.menu)
        return await message.answer(str(exc), reply_markup=operation_menu("bán"))
    await state.clear(); totals = await sales_totals(session, user.id, Period.TODAY)
    result_lines = [
        f"• {result.product_name} {result.display_size}: "
        f"đã bán {result.quantity_sold} {result.stock_unit}, còn {result.remaining} {result.stock_unit}"
        for result in results
    ]
    await message.answer(
        "✅ ĐÃ CHỐT BÁN HÀNG\n\n" + "\n".join(result_lines) + "\n\n"
        f"Doanh thu phiếu: {format_money(sum(result.revenue for result in results))}\n"
        f"Hoa hồng phiếu: {format_money(sum(result.commission for result in results))}\n"
        f"Trả công ty: {format_money(sum(result.company_amount for result in results))}\n\n"
        f"💵 DOANH THU HÔM NAY: {format_money(totals.revenue)}\n"
        f"💰 HOA HỒNG HÔM NAY: {format_money(totals.commission)}\n"
        "📦 Tồn kho đã cập nhật theo từng sản phẩm.",
        reply_markup=user_menu(),
    )
