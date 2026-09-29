import asyncio
from pathlib import Path
import tempfile

from aiogram.types import User as TelegramUser
from sqlalchemy import func, select, text

from app.database import create_database, initialize_database
from app.handlers.admin import admin_role_list_keyboard
from app.models import AdminAuditLog, CommissionType, Product, User, UserRole
from app.services.inventory_service import import_products, list_inventory
from app.services.user_service import decide_reactivation, register_or_update_user, request_reactivation, soft_delete_user
from app.utils.permissions import get_unblocked_user


def telegram_user(*, username: str = "nguyenvana") -> TelegramUser:
    return TelegramUser(
        id=123456789, is_bot=False, first_name="Nguyễn Văn", last_name="A",
        username=username, language_code="vi",
    )


def test_self_registration_soft_delete_and_data_isolation() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(
                f"sqlite+aiosqlite:///{(Path(temp_dir) / 'users.db').as_posix()}"
            )
            await initialize_database(engine)
            async with factory() as session:
                user, created = await register_or_update_user(session, telegram_user())
                assert created is True
                assert user.username == "nguyenvana"
                assert user.display_name == "Nguyễn Văn A"

                same_user, created_again = await register_or_update_user(
                    session, telegram_user(username="nguyenvana_moi")
                )
                assert created_again is False
                assert same_user.id == user.id
                assert same_user.username == "nguyenvana_moi"
                assert await session.scalar(select(func.count(User.id))) == 1

                other = User(telegram_id=987654321, first_name="Trần Văn", last_name="B")
                session.add(other)
                await session.commit()
                product = Product(name="Coca", variant="330", unit="ml", price=12000,
                                  commission_type=CommissionType.PERCENT, commission_value=1000)
                session.add(product)
                await session.commit()
                await import_products(session, user.id, [(product.id, 10)])
                assert len(await list_inventory(session, user.id)) == 1
                assert await list_inventory(session, other.id) == []

                owner = User(telegram_id=999, first_name="Owner", role=UserRole.SUPER_ADMIN)
                session.add(owner); await session.commit()
                inventory_count = len(await list_inventory(session, user.id))
                await soft_delete_user(session, owner, user)
                assert await get_unblocked_user(session, user.telegram_id) is None
                assert user.is_deleted is True and user.deleted_by_user_id == owner.id
                same_deleted, recreated = await register_or_update_user(session, telegram_user(username="deleted"))
                assert recreated is False and same_deleted.id == user.id and same_deleted.is_deleted is True
                assert await session.scalar(select(func.count(User.id))) == 3
                assert len(await list_inventory(session, user.id)) == inventory_count
                assert await session.scalar(
                    select(func.count(AdminAuditLog.id)).where(AdminAuditLog.action == "SOFT_DELETE_USER")
                ) == 1
                original = (user.id, user.telegram_id, user.role, user.manager_admin_id)
                assert await request_reactivation(session, user) is True
                assert await request_reactivation(session, user) is False
                assert user.reactivation_requested_at is not None
                try:
                    await soft_delete_user(session, other, owner)
                    raise AssertionError("ADMIN/USER không được xóa tài khoản")
                except PermissionError:
                    pass
                try:
                    await soft_delete_user(session, owner, owner)
                    raise AssertionError("Không được xóa SUPER_ADMIN")
                except ValueError:
                    pass
                try:
                    await decide_reactivation(session, other, user, approve=True)
                    raise AssertionError("USER không được duyệt lại tài khoản")
                except PermissionError:
                    pass
                await decide_reactivation(session, owner, user, approve=False)
                assert user.is_deleted is True and user.reactivation_requested_at is None
                assert await request_reactivation(session, user) is True
                await decide_reactivation(session, owner, user, approve=True)
                assert user.is_deleted is False and user.reactivation_requested_at is None
                assert (user.id, user.telegram_id, user.role, user.manager_admin_id) == original
                assert len(await list_inventory(session, user.id)) == inventory_count
            await engine.dispose()
    asyncio.run(scenario())


def test_legacy_users_table_migration() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(
                f"sqlite+aiosqlite:///{(Path(temp_dir) / 'legacy.db').as_posix()}"
            )
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL UNIQUE, "
                    "full_name VARCHAR(200) NOT NULL, active BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO users VALUES (1, 111, 'Người Cũ', 0, '2026-09-15 10:00:00')"
                )
                await connection.exec_driver_sql(
                    "CREATE TABLE legacy_child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL "
                    "REFERENCES users(id) ON DELETE CASCADE)"
                )
                await connection.exec_driver_sql("INSERT INTO legacy_child VALUES (1, 1)")
            await initialize_database(engine)
            async with factory() as session:
                migrated = await session.scalar(select(User).where(User.telegram_id == 111))
                assert migrated.first_name == "Người Cũ"
                assert migrated.is_blocked is True
                assert migrated.last_active_at is not None
                fk_target = (await session.execute(
                    text("PRAGMA foreign_key_list(legacy_child)")
                )).one()[2]
                assert fk_target == "users"
                assert (await session.execute(
                    text("PRAGMA foreign_key_check")
                )).all() == []
            await engine.dispose()
    asyncio.run(scenario())


def test_super_admin_sync_and_role_defaults() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(temp_dir) / 'roles.db').as_posix()}")
            await initialize_database(engine)
            async with factory() as session:
                session.add_all([
                    User(telegram_id=100, first_name="Owner"),
                    User(telegram_id=200, first_name="Worker 1"),
                    User(telegram_id=201, first_name="Worker 2", is_blocked=True),
                    User(telegram_id=202, first_name="Worker 3"),
                    User(telegram_id=203, first_name="Worker 4"),
                ])
                await session.commit()
            await initialize_database(engine, frozenset({100}))
            async with factory() as session:
                owner = await session.scalar(select(User).where(User.telegram_id == 100))
                worker = await session.scalar(select(User).where(User.telegram_id == 200))
                assert owner.role == UserRole.SUPER_ADMIN and owner.is_blocked is False
                assert worker.role == UserRole.USER
                result = await session.execute(select(User).where(User.role == UserRole.USER).order_by(User.id.asc()))
                promotable = list(result.scalars().all())
                assert len(promotable) == 4
                keyboard = admin_role_list_keyboard(promotable, "promote", 0, 1)
                callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row
                             if (button.callback_data or "").startswith("roles:do:promote:")]
                assert callbacks == [f"roles:do:promote:{user.id}" for user in promotable]
                assert len(set(callbacks)) == 4
                worker.role = UserRole.ADMIN; worker.promoted_by = owner.id
                session.add(AdminAuditLog(admin_user_id=owner.id, action="PROMOTE_ADMIN", target_type="USER",
                                          target_id=worker.id, old_data='{"role":"USER"}', new_data='{"role":"ADMIN"}'))
                await session.commit()
                assert await session.scalar(select(func.count(AdminAuditLog.id))) == 1
                remaining = list((await session.execute(select(User).where(User.role == UserRole.USER))).scalars().all())
                assert len(remaining) == 3
            await engine.dispose()
    asyncio.run(scenario())
