from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.inventory import require_employee
from app.keyboards.inventory import period_keyboard
from app.models import CommissionType, TransactionType, UserProductCommission
from app.services.product_service import all_products
from app.utils.money import format_commission, format_decimal
from sqlalchemy import select
from app.services.statistics_service import Period, sales_totals, transaction_history
from app.services.inventory_service import list_inventory
from app.utils.money import format_money

router = Router(name="statistics")

PERIOD_NAMES = {
    Period.TODAY: "Hôm nay", Period.SEVEN_DAYS: "7 ngày", Period.MONTH: "Tháng này", Period.ALL: "Tất cả"
}


@router.message(F.text == "💰 Hoa hồng của bạn")
async def my_commissions(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user: return
    lines = []
    for product in await all_products(session):
        custom = await session.scalar(select(UserProductCommission).where(
            UserProductCommission.user_id == user.id, UserProductCommission.product_id == product.id))
        kind = custom.commission_type if custom else product.commission_type
        value = custom.commission_value if custom else product.commission_value
        label = format_commission(kind, value, percent_is_basis_points=True)
        lines.append(f"• {product.display_name}: {label}")
    await message.answer("💰 HOA HỒNG CỦA BẠN\n\n" + ("\n".join(lines) or "Chưa có sản phẩm."))


@router.message(F.text == "💰 Doanh thu & hoa hồng")
async def statistics_menu(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await require_employee(message, session, settings):
        return
    await message.answer("Chọn khoảng thời gian:", reply_markup=period_keyboard("stats"))


@router.callback_query(F.data.startswith("stats:"))
async def statistics_result(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(callback, session, settings)
    if not user:
        return
    try:
        period = Period(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        return await callback.answer("Khoảng thời gian không hợp lệ.", show_alert=True)
    totals = await sales_totals(session, user.id, period)
    await callback.message.edit_text(
        f"💵 DOANH THU — {PERIOD_NAMES[period]}\n\n"
        f"Doanh thu: {format_money(totals.revenue)}\n"
        f"💰 Hoa hồng: {format_money(totals.commission)}\n"
        f"🚚 Tiền ship: {format_money(totals.shipping)}\n"
        f"🏢 Phải nộp: {format_money(totals.company)}"
    )
    await callback.answer()


@router.message(F.text == "📜 Lịch sử")
async def history_menu(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not await require_employee(message, session, settings):
        return
    await message.answer("Chọn khoảng thời gian xem lịch sử:", reply_markup=period_keyboard("history", history=True))


@router.callback_query(F.data.startswith("history:"))
async def history_result(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(callback, session, settings)
    if not user:
        return
    try:
        period = Period(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        return await callback.answer("Khoảng thời gian không hợp lệ.", show_alert=True)
    rows = await transaction_history(session, user.id, period)
    totals = await sales_totals(session, user.id, period)
    inventory = await list_inventory(session, user.id)
    lines = [f"📜 LỊCH SỬ — {PERIOD_NAMES[period]}"]
    if not rows:
        lines.append("\nChưa có giao dịch kho.")
    for transaction, product in rows:
        icon = "📥" if transaction.transaction_type == TransactionType.IMPORT else "🛒"
        sign = "+" if transaction.quantity > 0 else ""
        snapshot_name = transaction.product_name_snapshot or product.name
        snapshot_unit = transaction.product_unit_snapshot
        separator = "" if snapshot_unit.casefold() in {"ml", "cl", "dl", "l", "mg", "g", "kg"} else " "
        snapshot_size = f"{transaction.product_variant_snapshot}{separator}{snapshot_unit}".strip() or product.display_size
        stock_unit = transaction.product_stock_unit_snapshot or product.stock_unit or "sản phẩm"
        lines.append(
            f"\n{icon} {transaction.created_at:%d/%m/%Y %H:%M} — "
            f"{snapshot_name} {snapshot_size}: {sign}{format_decimal(transaction.quantity)} {stock_unit}"
        )
    lines.extend([
        "\n━━━━━━━━━━━━", f"Doanh thu: {format_money(totals.revenue)}",
        f"Hoa hồng: {format_money(totals.commission)}",
        f"Trả công ty: {format_money(totals.company)}",
        f"Số loại đang còn tồn: {len(inventory)}",
    ])
    text = "\n".join(lines)
    # Telegram giới hạn 4096 ký tự; lịch sử đã giới hạn 100 dòng và chia an toàn.
    for offset in range(0, len(text), 4000):
        await callback.message.answer(text[offset:offset + 4000])
    await callback.answer()
