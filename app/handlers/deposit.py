from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from payos import APIError, AsyncPayOS
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.inventory import require_employee
from app.keyboards.deposit import deposit_actions
from app.models import DepositStatus
from app.services.deposit_service import (
    create_deposit, mark_cancelled, mark_deposit_failed, mark_link_created,
    owned_deposit, process_successful_deposit,
)
from app.services.payos_service import (
    cancel_payment, create_payment_link, get_payment, payment_data_dict,
    payos_is_configured,
)
from app.services.wallet_service import wallet_for_user
from app.utils.money import format_money
from app.utils.parser import parse_integer

router = Router(name="deposit")


class DepositState(StatesGroup):
    entering_amount = State()


STATUS_TEXT = {
    DepositStatus.PENDING: "🟡 Chờ thanh toán",
    DepositStatus.PAID: "✅ Đã thanh toán",
    DepositStatus.CANCELLED: "❌ Đã hủy",
    DepositStatus.EXPIRED: "⌛ Đã hết hạn",
    DepositStatus.FAILED: "❌ Thất bại",
    DepositStatus.REVIEW: "⚠️ Cần kiểm tra",
}


def deposit_message(deposit, expires_minutes: int) -> str:
    return (
        "💵 NẠP TIỀN\n\n"
        f"Số tiền: {format_money(deposit.amount)}\n\n"
        f"Trạng thái: {STATUS_TEXT[deposit.status]}\n\n"
        "Ngân hàng nhận: BIDV\n"
        f"Thời hạn: {expires_minutes} phút\n\n"
        f"Mã đơn: {deposit.order_code}"
    )


@router.message(F.text == "💵 Nạp tiền")
async def deposit_start(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if not await require_employee(message, session, settings):
        return
    if not payos_is_configured(settings):
        return await message.answer("Chức năng payOS chưa được Admin cấu hình đầy đủ.")
    await state.set_state(DepositState.entering_amount)
    await message.answer(
        "Nhập số tiền muốn nạp (tối thiểu 1.000đ):\n\n"
        "Ví dụ: 500000, 500.000 hoặc 500,000"
    )


@router.message(DepositState.entering_amount)
async def deposit_create(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    payos: AsyncPayOS,
) -> None:
    user = await require_employee(message, session, settings)
    if not user:
        await state.clear()
        return
    try:
        amount = parse_integer(message.text or "", "Số tiền nạp")
        deposit = await create_deposit(
            session, user.id, amount, settings.payos_payment_expires_minutes
        )
    except ValueError as exc:
        return await message.answer(f"❌ {exc}")
    try:
        link = await create_payment_link(payos, settings, deposit)
        deposit = await mark_link_created(
            session,
            deposit.id,
            payment_link_id=link.payment_link_id,
            checkout_url=link.checkout_url,
            qr_code=link.qr_code,
        )
    except APIError:
        await mark_deposit_failed(session, deposit.id)
        await state.clear()
        return await message.answer("❌ payOS chưa tạo được link thanh toán. Vui lòng thử lại sau.")
    except Exception:
        await mark_deposit_failed(session, deposit.id)
        await state.clear()
        return await message.answer("❌ Không thể kết nối payOS. Vui lòng thử lại sau.")
    await state.clear()
    await message.answer(
        deposit_message(deposit, settings.payos_payment_expires_minutes),
        reply_markup=deposit_actions(deposit.id, deposit.checkout_url),
    )


@router.callback_query(F.data.startswith("deposit:"))
async def deposit_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    payos: AsyncPayOS,
) -> None:
    user = await require_employee(callback, session, settings)
    if not user:
        return
    try:
        _, action, raw_id = callback.data.split(":", 2)
        deposit_id = int(raw_id)
    except (ValueError, AttributeError):
        return await callback.answer("Dữ liệu yêu cầu nạp không hợp lệ.", show_alert=True)
    deposit = await owned_deposit(session, deposit_id, user.id)
    if deposit is None:
        return await callback.answer("Yêu cầu nạp không thuộc tài khoản của bạn.", show_alert=True)

    if action == "cancel":
        if deposit.status != DepositStatus.PENDING:
            return await callback.answer("Yêu cầu này không còn có thể hủy.", show_alert=True)
        try:
            await cancel_payment(payos, deposit)
        except Exception:
            return await callback.answer("payOS chưa xác nhận hủy. Hãy kiểm tra lại giao dịch.", show_alert=True)
        if await mark_cancelled(session, deposit.id, user.id):
            deposit.status = DepositStatus.CANCELLED
            await callback.message.edit_text(
                deposit_message(deposit, settings.payos_payment_expires_minutes)
            )
            return await callback.answer("Đã hủy yêu cầu trên payOS.")
        return await callback.answer("Yêu cầu này không còn có thể hủy.", show_alert=True)

    if action != "check":
        return await callback.answer("Thao tác không hợp lệ.", show_alert=True)
    try:
        payment = await get_payment(payos, deposit)
    except Exception:
        return await callback.answer("Không lấy được trạng thái từ payOS.", show_alert=True)

    if payment.status == "PAID":
        current_deposit_id = deposit.id
        current_user_id = user.id
        transaction = next(
            (item for item in payment.transactions if item.amount == deposit.amount),
            payment.transactions[0] if payment.transactions else None,
        )
        if transaction is None:
            return await callback.answer("payOS báo PAID nhưng chưa có giao dịch để xác minh.", show_alert=True)
        result = await process_successful_deposit(
            session,
            order_code=payment.order_code,
            amount=transaction.amount,
            reference=transaction.reference,
            payment_link_id=payment.id,
            raw_data=payment_data_dict(payment),
        )
        deposit = await owned_deposit(session, current_deposit_id, current_user_id)
        await callback.message.edit_text(
            deposit_message(deposit, settings.payos_payment_expires_minutes)
        )
        return await callback.answer(
            "Đã cộng số dư." if result.outcome == "PAID" else "Giao dịch đã được xử lý trước đó."
        )

    provider_statuses = {
        "CANCELLED": DepositStatus.CANCELLED,
        "EXPIRED": DepositStatus.EXPIRED,
        "FAILED": DepositStatus.FAILED,
    }
    if payment.status in provider_statuses and deposit.status == DepositStatus.PENDING:
        deposit.status = provider_statuses[payment.status]
        await session.commit()
    deposit = await owned_deposit(session, deposit.id, user.id)
    markup = (
        deposit_actions(deposit.id, deposit.checkout_url)
        if deposit.status == DepositStatus.PENDING and deposit.checkout_url else None
    )
    await callback.message.edit_text(
        deposit_message(deposit, settings.payos_payment_expires_minutes),
        reply_markup=markup,
    )
    await callback.answer("Đã đồng bộ trạng thái trực tiếp từ payOS.")


@router.message(F.text.in_({"💳 Ví của tôi", "💳 Số dư ví"}))
async def wallet_balance(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await require_employee(message, session, settings)
    if not user:
        return
    wallet = await wallet_for_user(session, user.id)
    await message.answer(
        "💳 VÍ CỦA TÔI\n\n"
        f"Số dư: {format_money(wallet.balance if wallet else 0)}\n"
        f"Tổng đã nạp: {format_money(wallet.total_deposited if wallet else 0)}"
    )
