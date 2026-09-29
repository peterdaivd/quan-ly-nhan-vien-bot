from aiogram import Router

from app.handlers import admin, common, deposit, inventory, sales, statistics, user


def build_router() -> Router:
    router = Router(name="root")
    router.include_routers(common.router, admin.router, deposit.router, inventory.router, sales.router, statistics.router, user.router)
    return router
