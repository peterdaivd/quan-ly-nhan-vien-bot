from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserRole


async def get_user_by_telegram(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def is_staff(session: AsyncSession, telegram_id: int, super_ids=frozenset()) -> bool:
    if telegram_id in super_ids: return True
    user = await get_user_by_telegram(session, telegram_id)
    return bool(user and not user.is_deleted and user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN})


async def is_super_admin(session: AsyncSession, telegram_id: int, super_ids=frozenset()) -> bool:
    if telegram_id in super_ids: return True
    user = await get_user_by_telegram(session, telegram_id)
    return bool(user and not user.is_deleted and user.role == UserRole.SUPER_ADMIN)


def can_manage_employee(current_user: User, employee: User) -> bool:
    if current_user.role == UserRole.SUPER_ADMIN:
        return employee.role == UserRole.USER
    return current_user.role == UserRole.ADMIN and employee.role == UserRole.USER and employee.manager_admin_id == current_user.id


def can_edit_commission(current_user: User) -> bool:
    return current_user.role == UserRole.SUPER_ADMIN and not current_user.is_deleted


async def get_unblocked_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(
        select(User).where(User.telegram_id == telegram_id, User.is_deleted.is_(False))
    )


async def get_owned_record(session: AsyncSession, model, record_id: int, user_id: int):
    return await session.scalar(
        select(model).where(model.id == record_id, model.user_id == user_id)
    )
