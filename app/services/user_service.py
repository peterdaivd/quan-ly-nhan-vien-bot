from __future__ import annotations

from datetime import datetime

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.models import AdminAuditLog, UserRole
import json


async def register_or_update_user(
    session: AsyncSession, telegram_user: TelegramUser
) -> tuple[User, bool]:
    """Upsert bằng Telegram ID thực từ update; không nhận ID do người dùng nhập."""
    user = await session.scalar(
        select(User).where(User.telegram_id == telegram_user.id)
    )
    created = user is None
    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name or "",
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )
        session.add(user)
        try:
            await session.commit()
            return user, True
        except IntegrityError:
            # Hai update /start đồng thời vẫn chỉ tạo một bản ghi nhờ UNIQUE.
            await session.rollback()
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_user.id)
            )
            if user is None:
                raise
            created = False
    user.username = telegram_user.username
    user.first_name = telegram_user.first_name or ""
    user.last_name = telegram_user.last_name
    user.language_code = telegram_user.language_code
    user.last_active_at = datetime.now()
    await session.commit()
    return user, created


async def touch_user(session: AsyncSession, user: User) -> None:
    user.last_active_at = datetime.now()
    await session.commit()


async def soft_delete_user(session: AsyncSession, actor: User, target: User) -> None:
    if actor.role != UserRole.SUPER_ADMIN or actor.is_deleted:
        raise PermissionError("Chỉ SUPER_ADMIN được xóa người dùng.")
    if target.role == UserRole.SUPER_ADMIN:
        raise ValueError("❌ Không thể xóa Admin chính.")
    if target.is_deleted:
        raise ValueError("Tài khoản đã được xóa trước đó.")
    target.is_deleted = True
    target.deleted_at = datetime.now()
    target.deleted_by_user_id = actor.id
    session.add(AdminAuditLog(
        admin_user_id=actor.id, action="SOFT_DELETE_USER", target_type="USER", target_id=target.id,
        old_data=json.dumps({"is_deleted": False}), new_data=json.dumps({"is_deleted": True}),
    ))
    await session.commit()


async def request_reactivation(session: AsyncSession, user: User) -> bool:
    if not user.is_deleted:
        raise ValueError("Tài khoản đang hoạt động.")
    if user.reactivation_requested_at is not None:
        return False
    user.reactivation_requested_at = datetime.now()
    session.add(AdminAuditLog(
        admin_user_id=user.id, action="REQUEST_REACTIVATION", target_type="USER", target_id=user.id,
        old_data="{}", new_data=json.dumps({"requested": True}),
    ))
    await session.commit()
    return True


async def decide_reactivation(session: AsyncSession, actor: User, target: User, *, approve: bool) -> None:
    if actor.role != UserRole.SUPER_ADMIN or actor.is_deleted:
        raise PermissionError("Chỉ SUPER_ADMIN được xử lý yêu cầu.")
    if not target.is_deleted or target.reactivation_requested_at is None:
        raise ValueError("Yêu cầu không còn chờ duyệt.")
    action = "APPROVE_REACTIVATION" if approve else "REJECT_REACTIVATION"
    if approve:
        target.is_deleted = False
        target.deleted_at = None
        target.deleted_by_user_id = None
    target.reactivation_requested_at = None
    session.add(AdminAuditLog(
        admin_user_id=actor.id, action=action, target_type="USER", target_id=target.id,
        old_data=json.dumps({"is_deleted": True, "requested": True}),
        new_data=json.dumps({"is_deleted": target.is_deleted, "requested": False}),
    ))
    await session.commit()
