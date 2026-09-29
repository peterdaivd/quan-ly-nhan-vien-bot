from aiogram import Router

from app.handlers import admin, common, deposit, employee_history, inventory, sales, shipping, statistics, user


def build_router() -> Router:
    router = Router(name="root")
    router.include_routers(common.router, employee_history.router, shipping.router, admin.router, deposit.router, inventory.router, sales.router, statistics.router, user.router)
    return router
