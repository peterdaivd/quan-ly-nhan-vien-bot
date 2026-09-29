from __future__ import annotations

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, DailySale, Deposit, Product, StockTransaction, User, WalletTransaction


def create_database(database_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, future=True)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: AsyncEngine, super_admin_ids: frozenset[int] = frozenset()) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_migrate_legacy_users)
        await connection.run_sync(_migrate_legacy_payment_schema)
        await connection.run_sync(_migrate_product_catalog)
        await connection.run_sync(_migrate_product_stock_units)
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_migrate_user_roles)
        await connection.run_sync(_migrate_commissions)
        await connection.run_sync(_migrate_settlements)
        if super_admin_ids:
            ids = ",".join(str(int(value)) for value in super_admin_ids)
            # SUPER_ADMIN được xác định duy nhất từ .env. Các role SUPER_ADMIN cũ
            # không còn nằm trong cấu hình phải trở về USER để có thể được bổ nhiệm lại.
            await connection.exec_driver_sql(
                f"UPDATE users SET role='USER', promoted_by=NULL, promoted_at=NULL "
                f"WHERE role='SUPER_ADMIN' AND telegram_id NOT IN ({ids})"
            )
            await connection.exec_driver_sql(f"UPDATE users SET role='SUPER_ADMIN' WHERE telegram_id IN ({ids})")
            await connection.exec_driver_sql(
                f"UPDATE admin_payments SET receiver_user_id=(SELECT id FROM users WHERE telegram_id IN ({ids}) ORDER BY id LIMIT 1) "
                "WHERE receiver_user_id IS NULL"
            )


def _migrate_user_roles(connection) -> None:
    if "users" not in inspect(connection).get_table_names(): return
    columns = {column["name"] for column in inspect(connection).get_columns("users")}
    if "role" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN role VARCHAR(11) NOT NULL DEFAULT 'USER'")
    if "promoted_by" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN promoted_by INTEGER")
    if "promoted_at" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN promoted_at DATETIME")
    if "manager_admin_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN manager_admin_id INTEGER")
    if "is_deleted" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0")
    if "deleted_at" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN deleted_at DATETIME")
    if "deleted_by_user_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN deleted_by_user_id INTEGER")
    if "reactivation_requested_at" not in columns:
        connection.exec_driver_sql("ALTER TABLE users ADD COLUMN reactivation_requested_at DATETIME")


def _migrate_commissions(connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "daily_sales" in tables:
        columns = {column["name"] for column in inspector.get_columns("daily_sales")}
        if "seller_role" not in columns:
            connection.exec_driver_sql("ALTER TABLE daily_sales ADD COLUMN seller_role VARCHAR(11) NOT NULL DEFAULT 'USER'")
            connection.exec_driver_sql(
                "UPDATE daily_sales SET seller_role=COALESCE((SELECT role FROM users WHERE users.id=daily_sales.user_id), 'USER')"
            )
    if "employee_product_commissions" in tables:
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO user_product_commissions "
            "(id,user_id,product_id,commission_type,commission_value,created_by_super_admin_id,created_at,updated_at) "
            "SELECT id,user_id,product_id,commission_type,commission_value,created_by,created_at,updated_at "
            "FROM employee_product_commissions"
        )
        connection.exec_driver_sql("DROP TABLE employee_product_commissions")


def _migrate_settlements(connection) -> None:
    if "admin_payments" not in inspect(connection).get_table_names():
        return
    columns = {column["name"] for column in inspect(connection).get_columns("admin_payments")}
    if "payer_user_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE admin_payments ADD COLUMN payer_user_id INTEGER")
        connection.exec_driver_sql("UPDATE admin_payments SET payer_user_id=admin_user_id WHERE payer_user_id IS NULL")
    if "receiver_user_id" not in columns:
        connection.exec_driver_sql("ALTER TABLE admin_payments ADD COLUMN receiver_user_id INTEGER")
    if "payment_method" not in columns:
        connection.exec_driver_sql("ALTER TABLE admin_payments ADD COLUMN payment_method VARCHAR(20) NOT NULL DEFAULT 'PAYOS'")
    if "settlement_date" not in columns:
        connection.exec_driver_sql("ALTER TABLE admin_payments ADD COLUMN settlement_date DATE")
        connection.exec_driver_sql("UPDATE admin_payments SET settlement_date=DATE(created_at) WHERE settlement_date IS NULL")


def _migrate_legacy_users(connection) -> None:
    """Nâng bảng users cũ mà không làm mất dữ liệu và khóa ngoại liên quan."""
    inspector = inspect(connection)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "full_name" not in columns or "username" in columns:
        return

    # legacy_alter_table giữ các FK products/... trỏ tới tên `users` khi đổi tên tạm.
    connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.exec_driver_sql("ALTER TABLE users RENAME TO users_legacy")
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_users_telegram_id")
    User.__table__.create(connection)
    connection.exec_driver_sql(
        """
        INSERT INTO users (
            id, telegram_id, username, first_name, last_name, phone_number,
            language_code, is_blocked, created_at, last_active_at
        )
        SELECT id, telegram_id, NULL, full_name, NULL, NULL, NULL,
               CASE WHEN active = 1 THEN 0 ELSE 1 END,
               created_at, created_at
        FROM users_legacy
        """
    )
    connection.exec_driver_sql("DROP TABLE users_legacy")
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _drop_table_indexes(connection, table_name: str) -> None:
    for index in inspect(connection).get_indexes(table_name):
        name = index.get("name")
        if name:
            escaped = name.replace('"', '""')
            connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{escaped}"')


def _migrate_legacy_payment_schema(connection) -> None:
    """Thay schema notification cũ bằng payOS, không cho record cũ được cộng tiền."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "deposits" in tables:
        columns = {column["name"] for column in inspector.get_columns("deposits")}
        if "deposit_code" in columns:
            connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            _drop_table_indexes(connection, "deposits")
            connection.exec_driver_sql("ALTER TABLE deposits RENAME TO deposits_personal_legacy")
            Deposit.__table__.create(connection)
            connection.exec_driver_sql(
                """
                INSERT INTO deposits (
                    id, user_id, order_code, amount, status,
                    payos_payment_link_id, payos_reference, checkout_url, qr_code,
                    created_at, expires_at, paid_at
                )
                SELECT id, user_id, 900000000000000 + id, amount,
                       CASE
                           WHEN status = 'PAID' THEN 'PAID'
                           WHEN status = 'EXPIRED' THEN 'EXPIRED'
                           ELSE 'FAILED'
                       END,
                       NULL, NULL, NULL, NULL, created_at, expires_at, paid_at
                FROM deposits_personal_legacy
                """
            )
            connection.exec_driver_sql("DROP TABLE deposits_personal_legacy")

    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "wallet_transactions" in tables:
        columns = {column["name"] for column in inspector.get_columns("wallet_transactions")}
        if "type" in columns:
            connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            _drop_table_indexes(connection, "wallet_transactions")
            connection.exec_driver_sql(
                "ALTER TABLE wallet_transactions RENAME TO wallet_transactions_legacy"
            )
            WalletTransaction.__table__.create(connection)
            connection.exec_driver_sql(
                """
                INSERT INTO wallet_transactions (
                    id, user_id, transaction_type, amount, balance_before,
                    balance_after, provider, reference, deposit_id, description, created_at
                )
                SELECT id, user_id,
                       CASE WHEN type = 'PAYMENT' THEN 'COMPANY_PAYMENT' ELSE type END,
                       amount, balance_before, balance_after, 'LEGACY', reference_id,
                       NULL, description, created_at
                FROM wallet_transactions_legacy
                """
            )
            connection.exec_driver_sql("DROP TABLE wallet_transactions_legacy")

    # Dữ liệu notification/nonce không còn tham gia bất kỳ luồng thanh toán nào.
    connection.exec_driver_sql("DROP TABLE IF EXISTS incoming_payment_events")
    connection.exec_driver_sql("DROP TABLE IF EXISTS used_nonces")
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _migrate_product_catalog(connection) -> None:
    """Đổi sản phẩm theo-user thành danh mục chung và giữ nguyên toàn bộ khóa lịch sử."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "products" in tables:
        columns = {column["name"] for column in inspector.get_columns("products")}
        if "user_id" in columns or "product_name" in columns:
            connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            _drop_table_indexes(connection, "products")
            connection.exec_driver_sql("ALTER TABLE products RENAME TO products_legacy")
            Product.__table__.create(connection)
            connection.exec_driver_sql(
                """
                INSERT INTO products (
                    id, name, variant, unit, price, commission_type,
                    commission_value, active, created_at, updated_at
                )
                SELECT id, product_name, weight, '', price, commission_type,
                       commission_value, 1, created_at, created_at
                FROM products_legacy
                """
            )
            connection.exec_driver_sql("DROP TABLE products_legacy")

    inspector = inspect(connection)
    for table_name, model in (("stock_transactions", StockTransaction), ("daily_sales", DailySale)):
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        added: set[str] = set()
        if "unit_price" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN unit_price INTEGER NOT NULL DEFAULT 0")
            added.add("unit_price")
        if "commission_type" not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} ADD COLUMN commission_type VARCHAR(14) NOT NULL DEFAULT 'FIXED_PER_ITEM'"
            )
            added.add("commission_type")
        if "commission_value" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN commission_value INTEGER NOT NULL DEFAULT 0")
            added.add("commission_value")
        if "product_name_snapshot" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN product_name_snapshot VARCHAR(200) NOT NULL DEFAULT ''")
            added.add("product_name_snapshot")
        if "product_variant_snapshot" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN product_variant_snapshot VARCHAR(100) NOT NULL DEFAULT ''")
            added.add("product_variant_snapshot")
        if "product_unit_snapshot" not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN product_unit_snapshot VARCHAR(30) NOT NULL DEFAULT ''")
            added.add("product_unit_snapshot")
        expressions = {
            "unit_price": f"unit_price = COALESCE((SELECT price FROM products WHERE products.id = {table_name}.product_id), 0)",
            "commission_type": f"commission_type = COALESCE((SELECT commission_type FROM products WHERE products.id = {table_name}.product_id), 'FIXED_PER_ITEM')",
            "commission_value": f"commission_value = COALESCE((SELECT commission_value FROM products WHERE products.id = {table_name}.product_id), 0)",
            "product_name_snapshot": f"product_name_snapshot = COALESCE((SELECT name FROM products WHERE products.id = {table_name}.product_id), '')",
            "product_variant_snapshot": f"product_variant_snapshot = COALESCE((SELECT variant FROM products WHERE products.id = {table_name}.product_id), '')",
            "product_unit_snapshot": f"product_unit_snapshot = COALESCE((SELECT unit FROM products WHERE products.id = {table_name}.product_id), '')",
        }
        if added:
            assignments = ", ".join(expressions[name] for name in expressions if name in added)
            connection.exec_driver_sql(f"UPDATE {table_name} SET {assignments}")
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")


def _migrate_product_stock_units(connection) -> None:
    """Tách đơn vị quản lý khỏi quy cách bằng migration chỉ ADD COLUMN."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "products" in tables:
        columns = {column["name"] for column in inspector.get_columns("products")}
        if "stock_unit" not in columns:
            connection.exec_driver_sql("ALTER TABLE products ADD COLUMN stock_unit VARCHAR(30)")
    inspector = inspect(connection)
    for table_name in ("stock_transactions", "daily_sales"):
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "product_stock_unit_snapshot" not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} ADD COLUMN product_stock_unit_snapshot "
                "VARCHAR(30) NOT NULL DEFAULT ''"
            )
