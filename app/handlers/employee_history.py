from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services.employee_history_service import (
    MAX_HISTORY_RANGE_DAYS,
    VIETNAM_TZ,
    EmployeeHistory,
    can_view_employee_history,
    get_employee_day_history,
    get_employee_range_summary,
)
from app.utils.money import format_decimal, format_money


router = Router(name="employee_history")
logger = logging.getLogger(__name__)
DETAIL_MESSAGE_LIMIT = 3600
RANGE_DAYS_PER_PAGE = 5


class EmployeeHistoryStates(StatesGroup):
    waiting_single_date = State()
    waiting_from_date = State()
    waiting_to_date = State()


def parse_date(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _origin_callback(user_id: int, origin: str) -> str:
    if origin == "a":
        return f"admin:info:{user_id}"
    if origin == "d":
        return f"deleted:info:{user_id}"
    return f"team:user:{user_id}"


def history_menu_keyboard(user_id: int, origin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Hôm nay", callback_data=f"hist:t:{user_id}:{origin}")],
        [InlineKeyboardButton(text="⏮ Hôm qua", callback_data=f"hist:y:{user_id}:{origin}")],
        [InlineKeyboardButton(text="📆 Chọn 1 ngày", callback_data=f"hist:s:{user_id}:{origin}")],
        [InlineKeyboardButton(text="🗓 Từ ngày - đến ngày", callback_data=f"hist:r:{user_id}:{origin}")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data=_origin_callback(user_id, origin))],
    ])


def cancel_keyboard(user_id: int, origin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Hủy", callback_data=f"hist:m:{user_id}:{origin}")
    ]])


async def _authorized(
    session: AsyncSession, telegram_id: int, target_id: int
) -> tuple[User | None, User | None]:
    actor = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    target = await session.get(User, target_id)
    if not can_view_employee_history(actor, target):
        return actor, None
    return actor, target


async def _deny(callback: CallbackQuery) -> None:
    await callback.answer("Bạn không được xem lịch sử nhân viên này.", show_alert=True)


def _split_message(header: str, lines: list[str]) -> list[str]:
    pages: list[str] = []
    current = header
    for line in lines:
        addition = f"\n{line}"
        if len(current) + len(addition) > DETAIL_MESSAGE_LIMIT and current != header:
            pages.append(current.rstrip())
            current = header + addition
        else:
            current += addition
    pages.append(current.rstrip())
    return pages


def render_day_pages(history: EmployeeHistory) -> list[str]:
    day = history.start_date
    header = (
        "📊 LỊCH SỬ NHÂN VIÊN\n\n"
        f"👤 {history.user.display_name}\n"
        f"📅 Ngày: {day:%d/%m/%Y}\n"
    )
    imports = [item for item in history.products if item.import_qty]
    sales = [item for item in history.products if item.sale_qty]
    lines = ["━━━━━━━━━━━━━━", "", "📦 NHẬP HÀNG"]
    if imports:
        for item in imports:
            lines.extend(["", item.display_name])
            lines.extend(
                f"{event.created_at:%H:%M}  +{format_decimal(event.quantity)} {item.quantity_unit}"
                for event in item.events if event.event_type == "IMPORT"
            )
            lines.append(f"Tổng nhập: {format_decimal(item.import_qty)} {item.quantity_unit}")
    else:
        lines.extend(["", "Không phát sinh."])
    lines.extend(["", "━━━━━━━━━━━━━━", "", "🛒 BÁN HÀNG"])
    if sales:
        for item in sales:
            lines.extend(["", item.display_name])
            lines.extend(
                f"{event.created_at:%H:%M}  {format_decimal(event.quantity)} {item.quantity_unit}"
                for event in item.events if event.event_type == "SALE"
            )
            lines.append(f"Tổng bán: {format_decimal(item.sale_qty)} {item.quantity_unit}")
    else:
        lines.extend(["", "Không phát sinh."])
    lines.extend(["", "━━━━━━━━━━━━━━", "", "📊 THEO SẢN PHẨM"])
    if not history.products:
        lines.extend(["", "Không có dữ liệu tồn kho lịch sử."])
    for item in history.products:
        lines.extend([
            "",
            f"📦 {item.display_name}",
            f"Tồn đầu: {format_decimal(item.opening_stock)} {item.quantity_unit}",
            f"Nhập: {format_decimal(item.import_qty)} {item.quantity_unit}",
            f"Bán: {format_decimal(item.sale_qty)} {item.quantity_unit}",
        ])
        if item.adjustment_qty:
            lines.append(f"Điều chỉnh: {item.adjustment_qty:+d} {item.quantity_unit}")
        lines.extend([
            f"Tồn cuối: {format_decimal(item.closing_stock)} {item.quantity_unit}",
            f"Doanh thu: {format_money(item.revenue)}",
            f"Hoa hồng: {format_money(item.commission)}",
        ])
        if item.events:
            lines.append("Chi tiết:")
            icons = {"IMPORT": "+", "SALE": "", "ADJUSTMENT": ""}
            lines.extend(
                f"{event.created_at:%H:%M}  {icons[event.event_type]}{event.quantity:+d}".replace("++", "+")
                for event in item.events
            )
    lines.extend([
        "", "━━━━━━━━━━━━━━", "", "📊 TỔNG KẾT NGÀY",
        f"Doanh thu: {format_money(history.total_revenue)}",
        f"Hoa hồng: {format_money(history.total_commission)}",
        f"🚚 Tiền ship: {format_money(history.total_shipping)}",
        f"Phải nộp: {format_money(history.total_due)}",
    ])
    return _split_message(header, lines)


def day_keyboard(user_id: int, day: date, page: int, pages: int, origin: str) -> InlineKeyboardMarkup:
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"hist:d:{user_id}:{day.isoformat()}:{page-1}:{origin}"))
    navigation.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="hist:n"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"hist:d:{user_id}:{day.isoformat()}:{page+1}:{origin}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        navigation,
        [InlineKeyboardButton(text="⬅️ Chọn thời gian", callback_data=f"hist:m:{user_id}:{origin}")],
    ])


async def show_day(
    callback: CallbackQuery, session: AsyncSession, user_id: int, day: date, page: int, origin: str
) -> None:
    _, target = await _authorized(session, callback.from_user.id, user_id)
    if target is None:
        return await _deny(callback)
    pages = render_day_pages(await get_employee_day_history(session, target.id, day))
    page = max(0, min(page, len(pages) - 1))
    text = pages[page]
    if len(pages) > 1:
        text += f"\n\nTrang: {page+1}/{len(pages)}"
    await callback.message.edit_text(text, reply_markup=day_keyboard(target.id, day, page, len(pages), origin))
    logger.info("EMPLOYEE_HISTORY_VIEW actor=%s target=%s start=%s end=%s", callback.from_user.id, target.id, day, day)
    await callback.answer()


def _quantity_lines(
    items: tuple[tuple[str, str, int], ...], max_length: int = 140, quantity_prefix: str = ""
) -> str:
    if not items:
        return "0"
    text = "; ".join(
        f"{name}: {quantity_prefix}{format_decimal(quantity)} {unit}"
        for name, unit, quantity in items
    )
    return text if len(text) <= max_length else text[:max_length - 1].rstrip() + "…"


async def show_range(
    callback: CallbackQuery, session: AsyncSession, user_id: int,
    from_date: date, to_date: date, page: int, origin: str,
) -> None:
    _, target = await _authorized(session, callback.from_user.id, user_id)
    if target is None:
        return await _deny(callback)
    history, days = await get_employee_range_summary(session, target.id, from_date, to_date)
    pages = max(1, (len(days) + RANGE_DAYS_PER_PAGE - 1) // RANGE_DAYS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    visible = days[page * RANGE_DAYS_PER_PAGE:(page + 1) * RANGE_DAYS_PER_PAGE]
    lines = [
        "📊 LỊCH SỬ NHÂN VIÊN", "", f"👤 {target.display_name}",
        f"📅 {from_date:%d/%m/%Y} → {to_date:%d/%m/%Y}", "", "━━━━━━━━━━━━",
    ]
    for item in visible:
        lines.extend([
            "", f"📅 {item.day:%d/%m/%Y}",
            f"Nhập: {_quantity_lines(item.imports)}",
            f"Bán: {_quantity_lines(item.sales)}",
            f"Doanh thu: {format_money(item.revenue)}",
            f"Hoa hồng: {format_money(item.commission)}",
            f"Tiền ship: {format_money(item.shipping)}",
            f"Phải nộp: {format_money(item.due)}",
        ])
    import_days = [item for item in visible if item.imports]
    lines.extend(["", "━━━━━━━━━━━━", "", "📦 CÁC NGÀY PHÁT SINH NHẬP"])
    if not import_days:
        lines.extend(["", "Không phát sinh."])
    for item in import_days:
        lines.extend(["", f"{item.day:%d/%m/%Y}", _quantity_lines(item.imports, quantity_prefix="+")])
    lines.extend([
        "", "━━━━━━━━━━━━", "", f"💵 Tổng doanh thu: {format_money(history.total_revenue)}",
        f"💰 Tổng hoa hồng: {format_money(history.total_commission)}",
        f"🚚 Tổng tiền ship: {format_money(history.total_shipping)}",
        f"Phải nộp: {format_money(history.total_due)}", "", f"Trang: {page+1}/{pages}",
    ])
    day_buttons = [
        InlineKeyboardButton(
            text=f"{item.day:%d/%m}",
            callback_data=f"hist:d:{target.id}:{item.day.isoformat()}:0:{origin}",
        ) for item in visible
    ]
    rows = [day_buttons[index:index + 3] for index in range(0, len(day_buttons), 3)]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"hist:p:{target.id}:{from_date.isoformat()}:{to_date.isoformat()}:{page-1}:{origin}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="hist:n"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"hist:p:{target.id}:{from_date.isoformat()}:{to_date.isoformat()}:{page+1}:{origin}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Chọn thời gian", callback_data=f"hist:m:{target.id}:{origin}")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    logger.info("EMPLOYEE_HISTORY_VIEW actor=%s target=%s start=%s end=%s", callback.from_user.id, target.id, from_date, to_date)
    await callback.answer()


@router.callback_query(F.data == "hist:n")
async def history_noop(callback: CallbackQuery) -> None:
    await callback.answer("Đây là số trang hiện tại.")


@router.callback_query(F.data.regexp(r"^hist:m:\d+:[atd]$"))
async def history_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, _, raw_id, origin = callback.data.split(":")
    _, target = await _authorized(session, callback.from_user.id, int(raw_id))
    if target is None:
        return await _deny(callback)
    await state.clear()
    await callback.message.edit_text(
        f"📅 CHỌN THỜI GIAN\n\n👤 {target.display_name}",
        reply_markup=history_menu_keyboard(target.id, origin),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^hist:[ty]:\d+:[atd]$"))
async def history_quick_day(callback: CallbackQuery, session: AsyncSession) -> None:
    _, action, raw_id, origin = callback.data.split(":")
    day = datetime.now(VIETNAM_TZ).date()
    if action == "y":
        day -= timedelta(days=1)
    await show_day(callback, session, int(raw_id), day, 0, origin)


@router.callback_query(F.data.regexp(r"^hist:s:\d+:[atd]$"))
async def history_single_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, _, raw_id, origin = callback.data.split(":")
    _, target = await _authorized(session, callback.from_user.id, int(raw_id))
    if target is None:
        return await _deny(callback)
    await state.set_state(EmployeeHistoryStates.waiting_single_date)
    await state.update_data(history_user_id=target.id, history_origin=origin)
    await callback.message.edit_text(
        "📅 Nhập ngày cần xem:\n\nĐịnh dạng:\nDD/MM/YYYY\n\nVí dụ:\n29/09/2026",
        reply_markup=cancel_keyboard(target.id, origin),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^hist:r:\d+:[atd]$"))
async def history_range_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, _, raw_id, origin = callback.data.split(":")
    _, target = await _authorized(session, callback.from_user.id, int(raw_id))
    if target is None:
        return await _deny(callback)
    await state.set_state(EmployeeHistoryStates.waiting_from_date)
    await state.update_data(history_user_id=target.id, history_origin=origin)
    await callback.message.edit_text(
        "📅 Nhập ngày bắt đầu:\n\nDD/MM/YYYY",
        reply_markup=cancel_keyboard(target.id, origin),
    )
    await callback.answer()


@router.message(
    EmployeeHistoryStates.waiting_single_date,
    F.text.in_({"❌ Hủy", "⬅️ Quay lại"}),
)
@router.message(
    EmployeeHistoryStates.waiting_from_date,
    F.text.in_({"❌ Hủy", "⬅️ Quay lại"}),
)
@router.message(
    EmployeeHistoryStates.waiting_to_date,
    F.text.in_({"❌ Hủy", "⬅️ Quay lại"}),
)
async def history_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Đã hủy xem lịch sử.")


async def _message_target(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    user_id = int(data.get("history_user_id", 0))
    _, target = await _authorized(session, message.from_user.id, user_id)
    return data, target


@router.message(EmployeeHistoryStates.waiting_single_date)
async def history_single_date(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data, target = await _message_target(message, state, session)
    if target is None:
        await state.clear()
        return await message.answer("Bạn không được xem lịch sử nhân viên này.")
    day = parse_date(message.text)
    if day is None:
        return await message.answer("❌ Ngày không hợp lệ.\n\nVui lòng nhập theo định dạng:\n\nDD/MM/YYYY")
    if day > datetime.now(VIETNAM_TZ).date():
        return await message.answer("❌ Không thể xem lịch sử của ngày trong tương lai.")
    await state.clear()
    history = await get_employee_day_history(session, target.id, day)
    pages = render_day_pages(history)
    origin = data.get("history_origin", "t")
    await message.answer(pages[0], reply_markup=day_keyboard(target.id, day, 0, len(pages), origin))


@router.message(EmployeeHistoryStates.waiting_from_date)
async def history_from_date(message: Message, state: FSMContext, session: AsyncSession) -> None:
    _, target = await _message_target(message, state, session)
    if target is None:
        await state.clear()
        return await message.answer("Bạn không được xem lịch sử nhân viên này.")
    day = parse_date(message.text)
    if day is None:
        return await message.answer("❌ Ngày không hợp lệ.\n\nVui lòng nhập theo định dạng:\n\nDD/MM/YYYY")
    if day > datetime.now(VIETNAM_TZ).date():
        return await message.answer("❌ Không thể xem lịch sử của ngày trong tương lai.")
    await state.update_data(history_from_date=day.isoformat())
    await state.set_state(EmployeeHistoryStates.waiting_to_date)
    data = await state.get_data()
    await message.answer(
        "📅 Nhập ngày kết thúc:\n\nDD/MM/YYYY",
        reply_markup=cancel_keyboard(target.id, data.get("history_origin", "t")),
    )


@router.message(EmployeeHistoryStates.waiting_to_date)
async def history_to_date(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data, target = await _message_target(message, state, session)
    if target is None:
        await state.clear()
        return await message.answer("Bạn không được xem lịch sử nhân viên này.")
    to_date = parse_date(message.text)
    if to_date is None:
        return await message.answer("❌ Ngày không hợp lệ.\n\nVui lòng nhập theo định dạng:\n\nDD/MM/YYYY")
    if to_date > datetime.now(VIETNAM_TZ).date():
        return await message.answer("❌ Không thể xem lịch sử của ngày trong tương lai.")
    from_date = date.fromisoformat(data["history_from_date"])
    if from_date > to_date:
        return await message.answer("❌ Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.\n\nVui lòng nhập lại ngày kết thúc:")
    if (to_date - from_date).days + 1 > MAX_HISTORY_RANGE_DAYS:
        return await message.answer(f"❌ Khoảng thời gian tối đa là {MAX_HISTORY_RANGE_DAYS} ngày.\n\nVui lòng nhập lại ngày kết thúc:")
    await state.clear()
    history, days = await get_employee_range_summary(session, target.id, from_date, to_date)
    origin = data.get("history_origin", "t")
    # Message handlers cannot edit the user's message, so render the first range page here.
    lines = [
        "📊 LỊCH SỬ NHÂN VIÊN", "", f"👤 {target.display_name}",
        f"📅 {from_date:%d/%m/%Y} → {to_date:%d/%m/%Y}", "", "━━━━━━━━━━━━",
    ]
    visible = days[:RANGE_DAYS_PER_PAGE]
    for item in visible:
        lines.extend(["", f"📅 {item.day:%d/%m/%Y}", f"Nhập: {_quantity_lines(item.imports)}",
                      f"Bán: {_quantity_lines(item.sales)}", f"Doanh thu: {format_money(item.revenue)}",
                      f"Hoa hồng: {format_money(item.commission)}",
                      f"Tiền ship: {format_money(item.shipping)}", f"Phải nộp: {format_money(item.due)}"])
    lines.extend(["", "━━━━━━━━━━━━", "", "📦 CÁC NGÀY PHÁT SINH NHẬP"])
    import_days = [item for item in visible if item.imports]
    if not import_days:
        lines.extend(["", "Không phát sinh."])
    for item in import_days:
        lines.extend(["", f"{item.day:%d/%m/%Y}", _quantity_lines(item.imports, quantity_prefix="+")])
    page_count = max(1, (len(days) + RANGE_DAYS_PER_PAGE - 1) // RANGE_DAYS_PER_PAGE)
    lines.extend(["", "━━━━━━━━━━━━", "", f"💵 Tổng doanh thu: {format_money(history.total_revenue)}",
                  f"💰 Tổng hoa hồng: {format_money(history.total_commission)}",
                  f"🚚 Tổng tiền ship: {format_money(history.total_shipping)}",
                  f"Phải nộp: {format_money(history.total_due)}", "", f"Trang: 1/{page_count}"])
    buttons = [InlineKeyboardButton(text=f"{item.day:%d/%m}", callback_data=f"hist:d:{target.id}:{item.day.isoformat()}:0:{origin}") for item in visible]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    if page_count > 1:
        rows.append([InlineKeyboardButton(text="1/" + str(page_count), callback_data="hist:n"),
                     InlineKeyboardButton(text="➡️", callback_data=f"hist:p:{target.id}:{from_date.isoformat()}:{to_date.isoformat()}:1:{origin}")])
    rows.append([InlineKeyboardButton(text="⬅️ Chọn thời gian", callback_data=f"hist:m:{target.id}:{origin}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^hist:d:\d+:\d{4}-\d{2}-\d{2}:\d+:[atd]$"))
async def history_day_page(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, raw_id, raw_day, raw_page, origin = callback.data.split(":")
    await show_day(callback, session, int(raw_id), date.fromisoformat(raw_day), int(raw_page), origin)


@router.callback_query(F.data.regexp(r"^hist:p:\d+:\d{4}-\d{2}-\d{2}:\d{4}-\d{2}-\d{2}:\d+:[atd]$"))
async def history_range_page(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, raw_id, raw_from, raw_to, raw_page, origin = callback.data.split(":")
    await show_range(callback, session, int(raw_id), date.fromisoformat(raw_from), date.fromisoformat(raw_to), int(raw_page), origin)
