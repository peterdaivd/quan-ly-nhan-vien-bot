from aiogram import Bot, Dispatcher

from app.config import Settings
from app.database import create_database
from app.handlers import build_router
from app.middleware import DatabaseSessionMiddleware
from app.services.payos_service import create_payos_client


def create_bot(settings: Settings):
    # Không bật parse mode toàn cục vì tên nhân viên/sản phẩm là dữ liệu người dùng nhập.
    bot = Bot(settings.bot_token)
    payos = create_payos_client(settings)
    dispatcher = Dispatcher(settings=settings, payos=payos)
    engine, session_factory = create_database(settings.database_url)
    middleware = DatabaseSessionMiddleware(session_factory)
    dispatcher.message.middleware(middleware)
    dispatcher.callback_query.middleware(middleware)
    dispatcher.include_router(build_router())
    return bot, dispatcher, engine, session_factory, payos
