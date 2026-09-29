import asyncio
import logging

from app.bot import create_bot
from app.backend import create_backend_app, start_backend, stop_backend
from app.config import load_settings
from app.database import initialize_database


async def main() -> None:
    settings = load_settings()
    bot, dispatcher, engine, session_factory, payos = create_bot(settings)
    backend_server = None
    backend_task = None
    try:
        await initialize_database(engine, settings.super_admin_ids)
        backend_app = create_backend_app(settings, session_factory, bot, payos)
        backend_server, backend_task = await start_backend(backend_app, settings)
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        if backend_server is not None and backend_task is not None:
            await stop_backend(backend_server, backend_task)
        await payos.aclose()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot đã dừng.")
    except Exception:
        logging.exception("Bot dừng do lỗi không xử lý được.")
        raise
