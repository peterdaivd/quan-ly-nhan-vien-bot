from __future__ import annotations

from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.inventory import require_employee
from app.keyboards.user import user_menu
from app.services.admin_payment_service import get_payment_receiver
from app.services.shipping_service import get_shipping_fee, set_shipping_fee, vietnam_today
from app.utils.money import format_money
from app.utils.parser import parse_integer
from app.config import Settings


router = Router(name="shipping")


class ShippingStates(StatesGroup):
    waiting_date = State()
    waiting_amount = State()


def shipping_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Hôm nay", callback_data="ship:t")],
        [InlineKeyboardButton(text="⏮ Hôm qua", callback_data="ship:y")],
        [InlineKeyboardButton(text="📆 Chọn ngày", callback_data="ship:c")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="ship:back")],
    ])


def shipping_day_keyboard(day: date) -> InlineKeyboardMarkup:
    value = day.isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Nhập/Sửa tiền ship", callback_data=f"ship:e:{value}")],
        [InlineKeyboardButton(text="🗑 Đặt về 0đ", callback_data=f"ship:z:{value}")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="ship:menu")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Hủy", callback_data="ship:menu")
    ]])


def parse_business_date(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_callback_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def show_shipping_day(callback: CallbackQuery, session: AsyncSession, settings: Settings, day: date) -> None:
    user = await require_employee(callback, session, settings)
    if not user:
        return
    amount = await get_shipping_fee(session, user.id, day)
    await callback.message.edit_text(
        f"🚚 TIỀN SHIP\n\n📅 {day:%d/%m/%Y}\nTiền ship hiện tại: {format_money(amount)}",
        reply_markup=shipping_day_keyboard(day),
    )
    await callback.answer()


@router.message(F.text == "🚚 Tiền ship")
async def shipping_menu_message(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not await require_employee(message, session, settings):
        return
    await state.clear()
    await message.answer("🚚 TIỀN SHIP\n\nChọn ngày:", reply_markup=shipping_menu_keyboard())


@router.callback_query(F.data == "ship:menu")
async def shipping_menu_callback(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not await require_employee(callback, session, settings):
        return
    await state.clear()
    await callback.message.edit_text("🚚 TIỀN SHIP\n\nChọn ngày:", reply_markup=shipping_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.in_({"ship:t", "ship:y"}))
async def shipping_quick_day(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    day = vietnam_today()
    if callback.data == "ship:y":
        day -= timedelta(days=1)
    await show_shipping_day(callback, session, settings, day)


@router.callback_query(F.data == "ship:c")
async def shipping_choose_date(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not await require_employee(callback, session, settings):
        return
    await state.set_state(ShippingStates.waiting_date)
    await callback.message.edit_text(
        "📅 Nhập ngày cần khai báo tiền ship:\n\nDD/MM/YYYY",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(ShippingStates.waiting_date)
async def shipping_date_input(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    user = await require_employee(message, session, settings)
    if not user:
        await state.clear()
        return
    day = parse_business_date(message.text)
    if day is None:
        return await message.answer("❌ Ngày không hợp lệ.\n\nVui lòng nhập theo định dạng DD/MM/YYYY.")
    if day > vietnam_today():
        return await message.answer("❌ Không thể khai báo tiền ship cho ngày trong tương lai.")
    await state.clear()
    amount = await get_shipping_fee(session, user.id, day)
    await message.answer(
        f"🚚 TIỀN SHIP\n\n📅 {day:%d/%m/%Y}\nTiền ship hiện tại: {format_money(amount)}",
        reply_markup=shipping_day_keyboard(day),
    )


@router.callback_query(F.data.regexp(r"^ship:e:\d{4}-\d{2}-\d{2}$"))
async def shipping_edit_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not await require_employee(callback, session, settings):
        return
    day = parse_callback_date(callback.data.rsplit(":", 1)[1])
    if day is None:
        return await callback.answer("Ngày không hợp lệ.", show_alert=True)
    if day > vietnam_today():
        return await callback.answer("Không thể khai báo ngày trong tương lai.", show_alert=True)
    await state.set_state(ShippingStates.waiting_amount)
    await state.update_data(shipping_date=day.isoformat())
    await callback.message.edit_text(
        f"🚚 Nhập tổng tiền ship ngày {day:%d/%m/%Y}:\n\n"
        "Ví dụ: 30000, 30.000 hoặc 30,000",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(ShippingStates.waiting_amount)
async def shipping_amount_input(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    user = await require_employee(message, session, settings)
    if not user:
        await state.clear()
        return
    data = await state.get_data()
    try:
        day = date.fromisoformat(data["shipping_date"])
        amount = parse_integer(message.text or "", "Tiền ship", allow_zero=True)
    except (KeyError, ValueError) as exc:
        return await message.answer(f"❌ {exc}")
    await set_shipping_fee(session, user.id, day, amount)
    await state.clear()
    await message.answer(
        f"✅ Đã lưu tiền ship.\n\n📅 {day:%d/%m/%Y}\nTiền ship: {format_money(amount)}",
        reply_markup=shipping_day_keyboard(day),
    )


@router.callback_query(F.data.regexp(r"^ship:z:\d{4}-\d{2}-\d{2}$"))
async def shipping_zero(
    callback: CallbackQuery, session: AsyncSession, settings: Settings
) -> None:
    user = await require_employee(callback, session, settings)
    if not user:
        return
    day = parse_callback_date(callback.data.rsplit(":", 1)[1])
    if day is None:
        return await callback.answer("Ngày không hợp lệ.", show_alert=True)
    if day > vietnam_today():
        return await callback.answer("Không thể khai báo ngày trong tương lai.", show_alert=True)
    await set_shipping_fee(session, user.id, day, 0)
    await callback.message.edit_text(
        f"🚚 TIỀN SHIP\n\n📅 {day:%d/%m/%Y}\nTiền ship hiện tại: 0đ",
        reply_markup=shipping_day_keyboard(day),
    )
    await callback.answer("Đã đặt tiền ship về 0đ.")


@router.callback_query(F.data == "ship:back")
async def shipping_back(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    user = await require_employee(callback, session, settings)
    if not user:
        return
    await state.clear()
    receiver = await get_payment_receiver(session, user)
    payment_label = f"💸 Nộp tiền cho {receiver.display_name}" if receiver and user.manager_admin_id else "💸 Nộp tiền Admin tổng"
    await callback.message.edit_text("Đã quay lại menu chính.")
    await callback.message.answer("Menu chính", reply_markup=user_menu(payment_label))
    await callback.answer()
