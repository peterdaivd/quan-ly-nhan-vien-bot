from aiogram import F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.admin import admin_menu
from app.keyboards.user import user_menu
from app.services.user_service import register_or_update_user, request_reactivation
from app.models import User, UserRole
from app.services.admin_payment_service import get_payment_receiver

router = Router(name="common")


def reactivation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Yêu cầu sử dụng lại bot", callback_data="reactivate:request")
    ]])


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, command: CommandObject) -> None:
    await state.clear()
    user, created = await register_or_update_user(session, message.from_user)
    if message.from_user.id in settings.super_admin_ids and user.role != UserRole.SUPER_ADMIN:
        user.role = UserRole.SUPER_ADMIN; await session.commit()
    if user.is_deleted:
        pending = "\n\n⏳ Yêu cầu của bạn đang chờ Admin xét duyệt." if user.reactivation_requested_at else ""
        await message.answer(
            "🚫 TÀI KHOẢN CHƯA ĐƯỢC PHÉP SỬ DỤNG\n\n"
            "Tài khoản của bạn đã bị xóa khỏi danh sách sử dụng bot.\n\n"
            f"Bạn có thể gửi yêu cầu để Admin duyệt sử dụng lại.{pending}",
            reply_markup=reactivation_keyboard(),
        )
        return
    invite_message = ""
    if command.args and command.args.startswith("invite_") and user.role == UserRole.USER:
        try:
            manager_id = int(command.args.removeprefix("invite_"))
        except ValueError:
            manager_id = 0
        manager = await session.get(type(user), manager_id)
        if manager and manager.role == UserRole.ADMIN and not manager.is_deleted:
            if user.manager_admin_id is None:
                user.manager_admin_id = manager.id
                await session.commit()
                invite_message = f"\n\n✅ Bạn đã tham gia nhóm của Admin {manager.display_name}."
                for super_id in settings.super_admin_ids:
                    try:
                        await message.bot.send_message(super_id, "👤 NHÂN VIÊN MỚI\n\n"
                            f"Tên: {user.display_name}\nĐược mời bởi: {manager.display_name}\n"
                            "Hoa hồng: Chưa cấu hình riêng / theo mặc định")
                    except Exception:
                        pass
            elif user.manager_admin_id == manager.id:
                invite_message = f"\n\n✅ Bạn đang thuộc nhóm của Admin {manager.display_name}."
            else:
                invite_message = "\n\nℹ️ Bạn đã thuộc một Admin khác nên hệ thống không tự chuyển nhóm."
    if user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN}:
        object.__setattr__(settings, "admin_ids", settings.admin_ids | {message.from_user.id})
        await message.answer(f"Xin chào {user.role.value}.", reply_markup=admin_menu(super_admin=user.role == UserRole.SUPER_ADMIN))
        return
    username = f"@{user.username}" if user.username else "Chưa có"
    receiver = await get_payment_receiver(session, user)
    payment_label = f"💸 Nộp tiền cho {receiver.display_name}" if receiver and user.manager_admin_id else "💸 Nộp tiền Admin tổng"
    if created:
        await message.answer(
            f"👋 Xin chào {user.display_name}!\n\n"
            "Tài khoản của bạn đã được tạo tự động.\n\n"
            f"Telegram ID: {user.telegram_id}\nUsername: {username}\n\n"
            f"Bạn có thể bắt đầu sử dụng bot.{invite_message}",
            reply_markup=user_menu(payment_label),
        )
    else:
        await message.answer(f"👋 Xin chào {user.display_name}!{invite_message}", reply_markup=user_menu(payment_label))


@router.callback_query(F.data == "reactivate:request")
async def reactivate_request(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not user or not user.is_deleted:
        return await callback.answer("Tài khoản của bạn đang hoạt động.", show_alert=True)
    if not await request_reactivation(session, user):
        return await callback.answer("⏳ Yêu cầu của bạn đang chờ Admin xét duyệt.\n\nVui lòng chờ phản hồi.", show_alert=True)
    manager = await session.get(User, user.manager_admin_id) if user.manager_admin_id else None
    username = f"@{user.username}" if user.username else "Chưa có"
    deleted_at = user.deleted_at.strftime("%d/%m/%Y %H:%M") if user.deleted_at else "Không xác định"
    text = ("📨 YÊU CẦU SỬ DỤNG LẠI BOT\n\n"
            f"👤 Người dùng:\n{user.display_name}\n\nTelegram ID:\n{user.telegram_id}\n\n"
            f"Username:\n{username}\n\nVai trò trước đây:\n{user.role.value}\n\n"
            f"Quản lý trước đây:\n{manager.display_name if manager else 'Không có'}\n\n"
            f"Ngày bị xóa:\n{deleted_at}\n\nNgười dùng đang yêu cầu được sử dụng bot trở lại.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ DUYỆT", callback_data=f"reactivate:approve:{user.id}"),
        InlineKeyboardButton(text="❌ TỪ CHỐI", callback_data=f"reactivate:reject:{user.id}"),
    ]])
    for super_id in settings.super_admin_ids:
        try: await callback.bot.send_message(super_id, text, reply_markup=keyboard)
        except Exception: pass
    await callback.message.edit_text("✅ Đã gửi yêu cầu đến Admin chính.\n\nVui lòng chờ phản hồi.")
    await callback.answer()


@router.callback_query(F.data == "reactivate:menu")
async def reactivate_menu(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not user or user.is_deleted:
        return await callback.answer("Tài khoản chưa được duyệt.", show_alert=True)
    if user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN}:
        object.__setattr__(settings, "admin_ids", settings.admin_ids | {user.telegram_id})
    keyboard = admin_menu(super_admin=user.role == UserRole.SUPER_ADMIN) if user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN} else user_menu()
    await callback.message.answer("🏠 Menu chính", reply_markup=keyboard); await callback.answer()


@router.message(Command("id"))
async def show_telegram_id(message: Message) -> None:
    await message.answer(f"Telegram ID của bạn: {message.from_user.id}")


@router.message(Command("cancel"))
@router.message(F.text == "Hủy")
async def cancel(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    await state.clear()
    from app.utils.permissions import get_user_by_telegram
    user = await get_user_by_telegram(session, message.from_user.id)
    keyboard = admin_menu(super_admin=user.role == UserRole.SUPER_ADMIN) if user and user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN} else user_menu()
    await message.answer("Đã hủy thao tác.", reply_markup=keyboard)
