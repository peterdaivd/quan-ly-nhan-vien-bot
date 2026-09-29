from __future__ import annotations

from math import ceil

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.inventory import (
    PAGE_SIZE, cart_keyboard, final_confirmation_keyboard, operation_menu,
    product_grid, quantity_keyboard,
)
from app.keyboards.user import user_menu
from app.models import CommissionType, Product, User
from app.services.inventory_service import import_products, list_inventory
from app.services.product_service import active_products
from app.services.user_service import touch_user
from app.utils.money import format_commission, format_money
from app.utils.parser import parse_integer
from app.utils.permissions import get_unblocked_user

router = Router(name="inventory")
QUICK_AMOUNTS = {"1️⃣": 1, "2️⃣": 2, "3️⃣": 3, "5️⃣": 5, "🔟": 10}


class ImportState(StatesGroup):
    menu = State()
    products = State()
    quantity = State()
    confirming = State()


async def require_employee(event, session: AsyncSession, settings: Settings):
    telegram_id = event.from_user.id
    user = await get_unblocked_user(session, telegram_id)
    if user is None:
        text = ("❌ Tài khoản của bạn đã bị xóa khỏi hệ thống."
                if await session.scalar(select(User).where(User.telegram_id == telegram_id))
                else "Bạn chưa đăng ký. Vui lòng gửi /start trước.")
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None
    await touch_user(session, user)
    return user


def commission_text(product: Product) -> str:
    return format_commission(product.commission_type, product.commission_value, percent_is_basis_points=True)


def button_map(products) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in products:
        name = item.product.display_name if hasattr(item, "product") else item.display_name
        counts[name] = counts.get(name, 0) + 1
    result: dict[str, int] = {}
    for item in products:
        product = item.product if hasattr(item, "product") else item
        record_id = item.id if hasattr(item, "product") else product.id
        suffix = f" (#{record_id})" if counts[product.display_name] > 1 else ""
        result[f"🥤 {product.display_name}{suffix}"] = record_id
    return result


async def products_by_ids(session: AsyncSession, ids: list[int]) -> list[Product]:
    if not ids:
        return []
    found = {p.id: p for p in (await session.scalars(select(Product).where(Product.id.in_(ids)))).all()}
    return [found[product_id] for product_id in ids if product_id in found]


async def import_cart_text(session: AsyncSession, cart: dict[str, int], *, final: bool = False) -> str:
    ids = [int(key) for key in cart]
    products = await products_by_ids(session, ids)
    lines = ["📦 XÁC NHẬN NHẬP HÀNG" if final else "📋 DANH SÁCH ĐANG NHẬP"]
    total = 0
    for index, product in enumerate(products, 1):
        quantity = int(cart[str(product.id)]); total += quantity
        if final:
            lines.append(
                f"\n{index}. {product.display_name}\nGiá: {format_money(product.price)}\n"
                f"Số lượng nhận: {quantity}\nHoa hồng: {commission_text(product)}"
            )
        else:
            lines.append(f"\n{index}. {product.display_name} x {quantity}")
    lines.extend(["\n━━━━━━━━━━━━", f"Tổng số loại: {len(products)}", f"Tổng số lượng: {total}"])
    return "\n".join(lines)


async def show_import_menu(message: Message, state: FSMContext, text: str = "📦 NHẬP HÀNG\n\nHãy chọn thao tác:") -> None:
    await state.set_state(ImportState.menu)
    await message.answer(text, reply_markup=operation_menu("nhập"))


async def show_product_page(message: Message, state: FSMContext, session: AsyncSession, page: int = 0) -> None:
    products = await active_products(session)
    if not products:
        await state.set_state(ImportState.menu)
        return await message.answer("Hiện chưa có sản phẩm đang bán.", reply_markup=operation_menu("nhập"))
    pages = max(1, ceil(len(products) / PAGE_SIZE)); page = max(0, min(page, pages - 1))
    current = products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    mapping = button_map(current)
    await state.update_data(page=page, product_buttons=mapping)
    await state.set_state(ImportState.products)
    await message.answer(
        "📦 SẢN PHẨM\n\nChọn sản phẩm muốn nhận:",
        reply_markup=product_grid(list(mapping), page, pages),
    )


def quantity_text(product: Product, quantity: int) -> str:
    return (
        "📦 SẢN PHẨM ĐANG CHỌN\n\n"
        f"Tên: {product.name}\nQuy cách: {product.display_size or 'Không có'}\n"
        f"Giá: {format_money(product.price)}\nHoa hồng: {commission_text(product)}\n\n"
        f"Số lượng đang chọn: {quantity}"
    )


async def select_import_product(message: Message, state: FSMContext, session: AsyncSession, product_id: int) -> None:
    product = await session.get(Product, product_id)
    if product is None or not product.active:
        return await message.answer("Sản phẩm đã bị ẩn hoặc không tồn tại.")
    await state.set_state(ImportState.quantity)
    sent = await message.answer(quantity_text(product, 1), reply_markup=quantity_keyboard(1))
    await state.update_data(current_id=product.id, current_quantity=1, quantity_message_id=sent.message_id)


async def refresh_import_quantity(message: Message, state: FSMContext, session: AsyncSession, quantity: int) -> None:
    data = await state.get_data(); product = await session.get(Product, int(data.get("current_id", 0)))
    if product is None:
        await state.set_state(ImportState.products)
        return await message.answer("Sản phẩm không còn tồn tại.")
    await state.update_data(current_quantity=quantity)
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=int(data["quantity_message_id"]))
    except (TelegramBadRequest, KeyError):
        pass
    sent = await message.answer(quantity_text(product, quantity), reply_markup=quantity_keyboard(quantity))
    await state.update_data(quantity_message_id=sent.message_id)


@router.message(F.text.in_({"📦 Nhập hàng", "📦 Nhập hàng buổi sáng"}))
async def start_import(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if not await require_employee(message, session, settings): return
    await state.set_data({"cart": {}})
    await show_import_menu(message, state)


@router.message(ImportState.menu, F.text == "📦 Sản phẩm")
async def import_products_menu(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await show_product_page(message, state, session)


@router.message(ImportState.products, F.text == "◀️ Trang trước")
async def import_previous_page(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await show_product_page(message, state, session, int((await state.get_data()).get("page", 0)) - 1)


@router.message(ImportState.products, F.text == "Trang sau ▶️")
async def import_next_page(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await show_product_page(message, state, session, int((await state.get_data()).get("page", 0)) + 1)


@router.message(ImportState.products, F.text.startswith("📄 "))
async def import_page_indicator(message: Message) -> None:
    await message.answer("Đây là số trang hiện tại.")


@router.message(ImportState.products, F.text == "📋 Xem lựa chọn")
async def view_import_cart(message: Message, state: FSMContext, session: AsyncSession) -> None:
    cart = (await state.get_data()).get("cart", {})
    if not cart: return await message.answer("❌ Bạn chưa chọn sản phẩm nào.")
    await state.set_state(ImportState.menu)
    await message.answer(await import_cart_text(session, cart), reply_markup=cart_keyboard("nhập"))


@router.message(ImportState.products, F.text == "⬅️ Quay lại")
async def products_back_to_import(message: Message, state: FSMContext) -> None:
    await show_import_menu(message, state)


@router.message(ImportState.products)
async def choose_import_product(message: Message, state: FSMContext, session: AsyncSession) -> None:
    product_id = (await state.get_data()).get("product_buttons", {}).get(message.text or "")
    if product_id is None: return await message.answer("Hãy chọn sản phẩm bằng nút trên bàn phím.")
    await select_import_product(message, state, session, int(product_id))


@router.message(ImportState.quantity, F.text == "✅ Đưa ra sản phẩm")
async def add_import_item(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data(); product = await session.get(Product, int(data.get("current_id", 0)))
    if product is None or not product.active: return await message.answer("Sản phẩm đã bị ẩn hoặc không tồn tại.")
    cart = dict(data.get("cart", {})); quantity = int(data.get("current_quantity", 1))
    cart[str(product.id)] = int(cart.get(str(product.id), 0)) + quantity
    await state.update_data(cart=cart)
    text = (
        f"✅ Đã thêm sản phẩm\n\n{product.display_name}\nSố lượng thêm: {quantity}\n\n"
        f"Hiện tại bạn đang chọn:\n" + (await import_cart_text(session, cart)).split("\n━━━━━━━━━━━━", 1)[0].replace("📋 DANH SÁCH ĐANG NHẬP\n", "")
    )
    await show_import_menu(message, state, text)


@router.message(ImportState.quantity, F.text == "🗑 Xóa lựa chọn")
async def remove_current_import_item(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data(); cart = dict(data.get("cart", {})); cart.pop(str(data.get("current_id", "")), None)
    await state.update_data(cart=cart)
    await message.answer("Đã bỏ sản phẩm đang chọn.")
    await show_product_page(message, state, session, int(data.get("page", 0)))


@router.message(ImportState.quantity, F.text == "⬅️ Quay lại")
async def quantity_back_to_products(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await show_product_page(message, state, session, int((await state.get_data()).get("page", 0)))


@router.message(ImportState.quantity)
async def change_import_quantity(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data(); current = int(data.get("current_quantity", 1)); text = (message.text or "").strip()
    if text == "➖": quantity = max(1, current - 1)
    elif text == "➕": quantity = current + 1
    elif text in QUICK_AMOUNTS: quantity = current + QUICK_AMOUNTS[text]
    elif text.startswith("🔢 "): return
    else:
        try: quantity = parse_integer(text, "Số lượng")
        except ValueError as exc: return await message.answer(f"❌ {exc}")
    await refresh_import_quantity(message, state, session, quantity)


@router.message(ImportState.menu, F.text == "🗑 Xóa lựa chọn")
async def clear_import_cart(message: Message, state: FSMContext) -> None:
    await state.update_data(cart={})
    await show_import_menu(message, state, "🗑 Đã xóa toàn bộ lựa chọn nhập hàng.")


@router.message(ImportState.menu, F.text == "⬅️ Quay lại")
async def import_back_main(message: Message, state: FSMContext) -> None:
    await state.clear(); await message.answer("Menu chính", reply_markup=user_menu())


@router.message(ImportState.menu, F.text == "✅ Xác nhận nhập")
async def preview_import(message: Message, state: FSMContext, session: AsyncSession) -> None:
    cart = (await state.get_data()).get("cart", {})
    if not cart: return await message.answer("❌ Bạn chưa chọn sản phẩm.")
    await state.set_state(ImportState.confirming)
    await message.answer(await import_cart_text(session, cart, final=True), reply_markup=final_confirmation_keyboard())


@router.message(ImportState.confirming, F.text == "✏️ Chọn thêm sản phẩm")
async def import_choose_more(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await show_product_page(message, state, session)


@router.message(ImportState.confirming, F.text == "🗑 Xóa lựa chọn")
async def clear_confirming_import(message: Message, state: FSMContext) -> None:
    await state.update_data(cart={}); await show_import_menu(message, state, "🗑 Đã xóa toàn bộ lựa chọn nhập hàng.")


@router.message(ImportState.confirming, F.text == "❌ Hủy")
async def cancel_import(message: Message, state: FSMContext) -> None:
    await state.clear(); await message.answer("Đã hủy nhập hàng.", reply_markup=user_menu())


@router.message(ImportState.confirming, F.text == "✅ Xác nhận")
async def confirm_import(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: await state.clear(); return
    cart = (await state.get_data()).get("cart", {}); items = [(int(key), int(value)) for key, value in cart.items()]
    try:
        if not items: raise ValueError("Phiếu nhập đang trống.")
        await import_products(session, user.id, items)
    except ValueError as exc:
        await session.rollback(); await state.set_state(ImportState.menu)
        return await message.answer(f"❌ {exc}", reply_markup=operation_menu("nhập"))
    await state.clear()
    await message.answer(f"✅ Đã nhập {sum(q for _, q in items)} sản phẩm và cập nhật tồn kho.", reply_markup=user_menu())


@router.message(F.text == "📦 Hàng đang có")
async def show_inventory(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: return
    items = await list_inventory(session, user.id)
    if not items: return await message.answer("📦 Bạn chưa có hàng tồn.")
    lines = ["📦 HÀNG ĐANG CÓ", ""]
    for item in items: lines.extend([item.product.display_name, f"Tồn: {item.current_quantity}", ""])
    lines.extend(["Tổng:", f"{sum(x.current_quantity for x in items)} sản phẩm"])
    await message.answer("\n".join(lines))
