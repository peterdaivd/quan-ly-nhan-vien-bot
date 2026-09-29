from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import User


class DatabaseSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            try:
                telegram_user = getattr(event, "from_user", None)
                if telegram_user:
                    user = await session.scalar(select(User).where(User.telegram_id == telegram_user.id))
                    if user and user.is_deleted:
                        is_start = isinstance(event, Message) and (event.text or "").split(maxsplit=1)[0] == "/start"
                        is_request = isinstance(event, CallbackQuery) and event.data == "reactivate:request"
                        if is_start or is_request:
                            return await handler(event, data)
                        text = "❌ Tài khoản của bạn đã bị xóa khỏi hệ thống."
                        if isinstance(event, CallbackQuery):
                            await event.answer(text, show_alert=True)
                        elif isinstance(event, Message):
                            await event.answer(text)
                        return None
                return await handler(event, data)
            except Exception:
                await session.rollback()
                raise

