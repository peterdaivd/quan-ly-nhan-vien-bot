from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CommissionType(str, Enum):
    PERCENT = "PERCENT"
    FIXED_PER_ITEM = "FIXED_PER_ITEM"


class TransactionType(str, Enum):
    IMPORT = "IMPORT"
    SALE = "SALE"
    ADJUSTMENT = "ADJUSTMENT"


class DepositStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    REVIEW = "REVIEW"


class WalletTransactionType(str, Enum):
    DEPOSIT = "DEPOSIT"
    COMPANY_PAYMENT = "COMPANY_PAYMENT"
    REFUND = "REFUND"
    ADJUSTMENT = "ADJUSTMENT"


class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    USER = "USER"


class AdminPaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    last_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reactivation_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    role: Mapped[UserRole] = mapped_column(SqlEnum(UserRole), default=UserRole.USER, server_default="USER", index=True)
    promoted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manager_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())

    @property
    def display_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip() or "Người dùng Telegram"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    variant: Mapped[str] = mapped_column(String(100), default="", server_default="")
    unit: Mapped[str] = mapped_column(String(30), default="", server_default="")
    price: Mapped[int] = mapped_column(Integer)
    commission_type: Mapped[CommissionType] = mapped_column(SqlEnum(CommissionType))
    # Phần trăm lưu theo basis point của 1%: 10% = 1000; tiền cố định lưu VND.
    commission_value: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, server_default=func.now())

    inventory: Mapped[list["Inventory"]] = relationship(back_populates="product")

    @property
    def display_size(self) -> str:
        if not self.variant:
            return self.unit.strip()
        if not self.unit:
            return self.variant.strip()
        compact_units = {"ml", "cl", "dl", "l", "mg", "g", "kg"}
        separator = "" if self.unit.casefold() in compact_units else " "
        return f"{self.variant}{separator}{self.unit}".strip()

    @property
    def display_name(self) -> str:
        return f"{self.name} {self.display_size}".strip()


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_inventory_owner_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    current_quantity: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="inventory")


class StockTransaction(Base):
    __tablename__ = "stock_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    transaction_type: Mapped[TransactionType] = mapped_column(SqlEnum(TransactionType))
    # IMPORT dương, SALE âm, ADJUSTMENT có thể dương/âm nhưng không được làm tồn âm.
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    commission_type: Mapped[CommissionType] = mapped_column(SqlEnum(CommissionType), default=CommissionType.FIXED_PER_ITEM, server_default="FIXED_PER_ITEM")
    commission_value: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    product_name_snapshot: Mapped[str] = mapped_column(String(200), default="", server_default="")
    product_variant_snapshot: Mapped[str] = mapped_column(String(100), default="", server_default="")
    product_unit_snapshot: Mapped[str] = mapped_column(String(30), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), index=True)


class DailySale(Base):
    __tablename__ = "daily_sales"
    __table_args__ = (Index("ix_daily_sales_user_date", "user_id", "sale_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    seller_role: Mapped[UserRole] = mapped_column(SqlEnum(UserRole), default=UserRole.USER, server_default="USER")
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sale_date: Mapped[date] = mapped_column(Date, default=date.today)
    quantity_sold: Mapped[int] = mapped_column(Integer)
    revenue: Mapped[int] = mapped_column(Integer)
    commission_amount: Mapped[int] = mapped_column(Integer)
    company_amount: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    commission_type: Mapped[CommissionType] = mapped_column(SqlEnum(CommissionType), default=CommissionType.FIXED_PER_ITEM, server_default="FIXED_PER_ITEM")
    commission_value: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    product_name_snapshot: Mapped[str] = mapped_column(String(200), default="", server_default="")
    product_variant_snapshot: Mapped[str] = mapped_column(String(100), default="", server_default="")
    product_unit_snapshot: Mapped[str] = mapped_column(String(30), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), index=True)


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_deposited: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, server_default=func.now())


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    transaction_type: Mapped[WalletTransactionType] = mapped_column(SqlEnum(WalletTransactionType))
    amount: Mapped[int] = mapped_column(Integer)
    balance_before: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str] = mapped_column(String(100), index=True)
    deposit_id: Mapped[int | None] = mapped_column(ForeignKey("deposits.id", ondelete="SET NULL"), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), index=True)


class Deposit(Base):
    __tablename__ = "deposits"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    order_code: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[DepositStatus] = mapped_column(SqlEnum(DepositStatus), default=DepositStatus.PENDING, index=True)
    payos_payment_link_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    payos_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    checkout_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    qr_code: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), default="PAYOS")
    reference: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    order_code: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    raw_data: Mapped[str] = mapped_column(String(10000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), index=True)


class PaymentOrderSequence(Base):
    __tablename__ = "payment_order_sequences"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), index=True)


class AdminPayment(Base):
    __tablename__ = "admin_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    payer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    receiver_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    payment_method: Mapped[str] = mapped_column(String(20), default="PAYOS", server_default="PAYOS")
    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    order_code: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    payos_payment_link_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    payos_reference: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    checkout_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    qr_code: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[AdminPaymentStatus] = mapped_column(SqlEnum(AdminPaymentStatus), default=AdminPaymentStatus.PENDING, server_default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaymentReminderSchedule(Base):
    __tablename__ = "payment_reminder_schedules"
    __table_args__ = (UniqueConstraint("hour", "minute", name="uq_payment_reminder_time"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    hour: Mapped[int] = mapped_column(Integer)
    minute: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class PaymentReminderDelivery(Base):
    __tablename__ = "payment_reminder_deliveries"
    __table_args__ = (UniqueConstraint("schedule_id", "user_id", "settlement_date", name="uq_reminder_delivery"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("payment_reminder_schedules.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    settlement_date: Mapped[date] = mapped_column(Date, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())


class UserProductCommission(Base):
    __tablename__ = "user_product_commissions"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_user_product_commission"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    commission_type: Mapped[CommissionType] = mapped_column(SqlEnum(CommissionType))
    commission_value: Mapped[int] = mapped_column(Integer)
    created_by_super_admin_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, server_default=func.now())


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_data: Mapped[str] = mapped_column(String(5000), default="{}")
    new_data: Mapped[str] = mapped_column(String(5000), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, server_default=func.now(), index=True)


__all__ = [
    "AdminAuditLog", "AdminPayment", "AdminPaymentStatus", "Base", "CommissionType", "DailySale", "Deposit", "DepositStatus", "UserProductCommission",
    "Inventory", "PaymentEvent", "PaymentOrderSequence", "PaymentReminderDelivery", "PaymentReminderSchedule", "Product", "StockTransaction",
    "TransactionType", "User", "Wallet", "WalletTransaction",
    "UserRole", "WalletTransactionType",
]
