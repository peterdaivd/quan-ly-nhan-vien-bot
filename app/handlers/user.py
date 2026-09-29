from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.user import account_keyboard, contact_keyboard, user_menu
from app.models import User
from app.utils.permissions import get_unblocked_user

router = Router(name="user_fallback")


@router.message(F.text == "👤 Chế độ của tôi")
async def personal_mode(message: Message, session: AsyncSession) -> None:
    user = await get_unblocked_user(session, message.from_user.id)
    if not user: return await message.answer("Tài khoản không tồn tại hoặc đã bị xóa.")
    await message.answer("Đã chuyển sang chế độ cá nhân.", reply_markup=user_menu())


@router.message(F.text == "👤 Tài khoản")
async def show_account(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await get_unblocked_user(session, message.from_user.id)
    if user is None:
        return await message.answer("❌ Tài khoản không tồn tại hoặc đã bị xóa.")
    username = f"@{user.username}" if user.username else "Chưa có"
    await message.answer(
        "👤 TÀI KHOẢN\n\n"
        f"Họ tên: {user.display_name}\nTelegram ID: {user.telegram_id}\n"
        f"Username: {username}\nSố điện thoại: {user.phone_number or 'Chưa cập nhật'}",
        reply_markup=account_keyboard(),
    )


@router.message(F.text == "⬅️ Quay lại")
async def back_to_main(message: Message) -> None:
    await message.answer("Menu chính", reply_markup=user_menu())


@router.message(F.text == "📱 Cập nhật số điện thoại")
async def request_phone(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await get_unblocked_user(session, message.from_user.id)
    if user is None:
        existing = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        text = "❌ Tài khoản của bạn đã bị xóa khỏi hệ thống." if existing and existing.is_deleted else "Vui lòng gửi /start trước."
        return await message.answer(text)
    await message.answer(
        "Bạn có thể chia sẻ số điện thoại của chính mình. Việc này không bắt buộc.",
        reply_markup=contact_keyboard(),
    )


@router.message(F.contact)
async def save_phone(message: Message, session: AsyncSession, settings: Settings) -> None:
    user = await get_unblocked_user(session, message.from_user.id)
    if user is None:
        return await message.answer("❌ Tài khoản không tồn tại hoặc đã bị xóa.")
    if message.contact.user_id != message.from_user.id:
        return await message.answer(
            "❌ Chỉ được chia sẻ số điện thoại thuộc tài khoản Telegram của bạn.",
            reply_markup=user_menu(),
        )
    user.phone_number = message.contact.phone_number
    await session.commit()
    await message.answer("✅ Đã cập nhật số điện thoại.", reply_markup=user_menu())


@router.message()
async def unknown(message: Message) -> None:
    await message.answer("Mình chưa hiểu lựa chọn này. Hãy dùng các nút trong menu hoặc /start.")
