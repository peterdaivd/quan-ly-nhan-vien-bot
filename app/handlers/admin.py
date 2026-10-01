from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from payos import AsyncPayOS
from sqlalchemy import String, cast, func, or_, select
from datetime import datetime
import json
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.admin import admin_menu
from app.models import AdminAuditLog, AdminPayment, AdminPaymentStatus, CommissionType, DailySale, Deposit, Inventory, PaymentReminderSchedule, Product, StockTransaction, User, UserProductCommission, UserRole, Wallet, WalletTransaction
from app.services.admin_payment_service import (amount_due_to_receiver, confirm_manual_settlement,
    create_admin_payment, create_settlement, get_payment_receiver, mark_admin_link_created,
    daily_settlement, debt_summary_to_receiver, paid_admin_total, paid_between, personal_company_total,
    received_from_staff_total)
from app.services.debt_adjustment_service import can_reset_debt, reset_debt, reset_history
from app.services.payos_service import create_admin_payment_link, payos_is_configured
from app.services.product_service import all_products, delete_or_hide_product, split_size, validate_stock_unit
from app.services.shipping_service import get_shipping_total, vietnam_today
from app.services.user_service import decide_reactivation, soft_delete_user
from app.utils.money import format_commission, format_money
from app.utils.parser import parse_commission, parse_integer
from app.utils.permissions import can_edit_commission, can_manage_employee

router = Router(name="admin")
logger = logging.getLogger(__name__)


class AdminState(StatesGroup):
    searching_user = State()
    product_name = State()
    product_size = State()
    product_stock_unit = State()
    product_price = State()
    product_commission = State()
    product_edit = State()
    product_edit_confirm = State()
    user_commission = State()
    reminder_time = State()


PRODUCT_PAGE_SIZE = 8
ADMIN_USER_PAGE_SIZE = 8


def is_admin(telegram_id: int, settings: Settings) -> bool:
    return telegram_id in settings.admin_ids


async def deny(event: Message | CallbackQuery) -> None:
    text = "⛔ Chức năng chỉ dành cho Admin."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)


async def audit(session: AsyncSession, actor_telegram_id: int, action: str, target_type: str, target_id: int | None, old_data: dict, new_data: dict) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == actor_telegram_id))
    if actor:
        session.add(AdminAuditLog(admin_user_id=actor.id, action=action, target_type=target_type,
                                  target_id=target_id, old_data=json.dumps(old_data, ensure_ascii=False),
                                  new_data=json.dumps(new_data, ensure_ascii=False)))


def username_text(user: User) -> str:
    return f"@{user.username}" if user.username else "Chưa có"


def status_text(user: User) -> str:
    return "🗑 Đã xóa" if user.is_deleted else "✅ Hoạt động"


def user_summary(user: User) -> str:
    return (
        f"{user.display_name}\nID: {user.telegram_id}\n"
        f"Username: {username_text(user)}\nVai trò: {user.role.value}\nTrạng thái: {status_text(user)}"
    )


async def user_details(user: User, session: AsyncSession) -> str:
    manager = await session.get(User, user.manager_admin_id) if user.manager_admin_id else None
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
    revenue, commission, company = (await session.execute(select(
        func.coalesce(func.sum(DailySale.revenue), 0),
        func.coalesce(func.sum(DailySale.commission_amount), 0),
        func.coalesce(func.sum(DailySale.company_amount), 0),
    ).where(DailySale.user_id == user.id))).one()
    shipping = await get_shipping_total(session, user.id)
    gross_due = max(0, int(company) - shipping)
    receiver = await get_payment_receiver(session, user)
    debt = await debt_summary_to_receiver(session, user, receiver) if receiver else None
    stock_types = await session.scalar(select(func.count(Inventory.id)).where(
        Inventory.user_id == user.id, Inventory.current_quantity > 0
    ))
    wallet_history = list((await session.scalars(
        select(WalletTransaction).where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc()).limit(5)
    )).all())
    history_text = "\n".join(
        f"• {row.created_at:%d/%m %H:%M}: +{format_money(row.amount)} ({row.provider} / {row.reference})"
        for row in wallet_history
    ) or "Chưa có"
    deposits = list((await session.scalars(
        select(Deposit).where(Deposit.user_id == user.id)
        .order_by(Deposit.created_at.desc()).limit(5)
    )).all())
    deposit_text = "\n".join(
        f"• {row.created_at:%d/%m %H:%M}: {format_money(row.amount)} | PAYOS | BIDV | "
        f"{row.status.value} | {row.payos_reference or 'chưa có reference'}"
        for row in deposits
    ) or "Chưa có"
    return (
        "📊 THÔNG TIN NGƯỜI DÙNG\n\n"
        f"Họ tên: {user.display_name}\nTelegram ID: {user.telegram_id}\n"
        f"Username: {username_text(user)}\n"
        f"Vai trò: {user.role.value}\n"
        f"Quản lý bởi: {manager.display_name if manager else 'Không có'}\n"
        f"Số điện thoại: {user.phone_number or 'Chưa cập nhật'}\n"
        f"Ngôn ngữ: {user.language_code or 'Không xác định'}\n"
        f"Trạng thái: {status_text(user)}\n"
        f"Ngày tạo: {user.created_at:%d/%m/%Y %H:%M}\n"
        f"Hoạt động gần nhất: {user.last_active_at:%d/%m/%Y %H:%M}"
        f"\n\n💳 Số dư ví: {format_money(wallet.balance if wallet else 0)}"
        f"\nTổng đã nạp: {format_money(wallet.total_deposited if wallet else 0)}"
        f"\nDoanh thu: {format_money(int(revenue))}"
        f"\nHoa hồng: {format_money(int(commission))}"
        f"\n🚚 Tiền ship: {format_money(shipping)}"
        f"\nPhải nộp gốc: {format_money(debt.gross_due if debt else gross_due)}"
        f"\nĐã nộp: {format_money(debt.paid if debt else 0)}"
        f"\nĐã reset công nợ: {format_money(debt.adjusted if debt else 0)}"
        f"\nCòn phải nộp: {format_money(debt.outstanding if debt else 0)}"
        f"\nSố loại đang còn tồn: {int(stock_types or 0)}"
        f"\n\nLịch sử ví gần nhất:\n{history_text}"
        f"\n\nLịch sử nạp gần nhất:\n{deposit_text}"
    )


async def all_users(session: AsyncSession, *, include_deleted: bool = False) -> list[User]:
    query = select(User)
    if not include_deleted:
        query = query.where(User.is_deleted.is_(False))
    return list((await session.scalars(query.order_by(User.created_at.desc()))).all())


async def managed_users(session: AsyncSession, actor: User, manager_id: int | None = None) -> list[User]:
    query = select(User).where(User.role == UserRole.USER, User.is_deleted.is_(False))
    if actor.role == UserRole.ADMIN:
        query = query.where(User.manager_admin_id == actor.id)
    elif manager_id is not None:
        query = query.where(User.manager_admin_id == manager_id)
    return list((await session.scalars(query.order_by(User.first_name, User.id))).all())


async def employee_totals(session: AsyncSession, user_id: int) -> dict[str, int]:
    revenue, commission, company = (await session.execute(select(
        func.coalesce(func.sum(DailySale.revenue), 0),
        func.coalesce(func.sum(DailySale.commission_amount), 0), func.coalesce(func.sum(DailySale.company_amount), 0),
    ).where(DailySale.user_id == user_id))).one()
    shipping = await get_shipping_total(session, user_id)
    user = await session.get(User, user_id)
    receiver = await get_payment_receiver(session, user) if user else None
    debt = await debt_summary_to_receiver(session, user, receiver) if user and receiver else None
    return {"revenue": int(revenue), "commission": int(commission), "shipping": shipping,
            "company": max(0, int(company) - shipping), "paid": debt.paid if debt else 0,
            "adjusted": debt.adjusted if debt else 0, "outstanding": debt.outstanding if debt else 0}


async def group_totals(session: AsyncSession, users: list[User]) -> dict[str, int]:
    total = {"revenue": 0, "commission": 0, "shipping": 0, "company": 0,
             "paid": 0, "adjusted": 0, "outstanding": 0}
    for user in users:
        values = await employee_totals(session, user.id)
        for key in total: total[key] += values[key]
    return total


def group_text(users: list[User], total: dict[str, int]) -> str:
    return ("📊 TỔNG HỢP NHÓM\n\n" f"Nhân viên: {len(users)}\n\n"
            f"Tổng doanh thu: {format_money(total['revenue'])}\nTổng hoa hồng: {format_money(total['commission'])}\n\n"
            f"Tổng tiền ship: {format_money(total['shipping'])}\n"
            f"Tổng đã nộp: {format_money(total['paid'])}\n"
            f"Tổng đã reset: {format_money(total['adjusted'])}\n"
            f"💵 Tổng còn phải nộp: {format_money(total['outstanding'])}")


def super_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Bổ nhiệm Admin", callback_data="roles:list:promote:0")],
        [InlineKeyboardButton(text="👥 Danh sách Admin", callback_data="roles:list:admins:0")],
        [InlineKeyboardButton(text="📊 Hoạt động Admin", callback_data="roles:activity")],
        [InlineKeyboardButton(text="⬇️ Hạ quyền Admin", callback_data="roles:list:demote:0")],
    ])


def admin_role_list_keyboard(users: list[User], action: str, page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{user.display_name} ({user.telegram_id})",
        callback_data=f"roles:do:{action}:{user.id}",
    )] for user in users]
    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"roles:list:{action}:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="roles:noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"roles:list:{action}:{min(pages - 1, page + 1)}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Quay lại", callback_data="roles:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "👑 Quản lý Admin")
async def manage_admins(message: Message, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: return await deny(message)
    await message.answer("👑 QUẢN LÝ ADMIN", reply_markup=super_admin_keyboard())


@router.message(F.text.in_({"💰 Quản lý hoa hồng", "💰 Xem hoa hồng"}))
async def commission_users(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings): return await deny(message)
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    query = select(User).where(User.is_deleted.is_(False))
    if not actor or actor.role == UserRole.USER: return await deny(message)
    if actor.role == UserRole.ADMIN:
        query = query.where(or_(User.id == actor.id, User.manager_admin_id == actor.id))
    users = list((await session.scalars(query.order_by(User.first_name))).all())
    if not users: return await message.answer("Chưa có nhân viên.")
    icons = {UserRole.SUPER_ADMIN: "⭐", UserRole.ADMIN: "👑", UserRole.USER: "👤"}
    rows = [[InlineKeyboardButton(text=f"{icons[u.role]} {u.display_name} · {u.role.value}", callback_data=f"comm:user:{u.id}")] for u in users]
    title = "Chọn tài khoản để cấu hình hoa hồng:" if actor.role == UserRole.SUPER_ADMIN else "Chọn nhân viên để xem hoa hồng:"
    await message.answer(title, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("comm:user:"))
async def commission_products(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings): return await deny(callback)
    user_id = int(callback.data.rsplit(":", 1)[1]); user = await session.get(User, user_id)
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    allowed = bool(actor and user and (actor.role == UserRole.SUPER_ADMIN or user.id == actor.id or can_manage_employee(actor, user)))
    if not allowed: return await callback.answer("Bạn không được xem tài khoản này.", show_alert=True)
    products = await all_products(session)
    if actor.role == UserRole.ADMIN:
        lines = []
        for product in products:
            custom = await session.scalar(select(UserProductCommission).where(UserProductCommission.user_id == user.id, UserProductCommission.product_id == product.id))
            commission_type = custom.commission_type if custom else product.commission_type
            value = custom.commission_value if custom else product.commission_value
            label = format_commission(commission_type, value, percent_is_basis_points=True)
            lines.append(f"• {product.display_name}: {label}")
        await callback.message.edit_text(f"💰 HOA HỒNG — {user.display_name}\n\n" + ("\n".join(lines) or "Chưa có sản phẩm.")); return await callback.answer()
    rows = [[InlineKeyboardButton(text=p.display_name, callback_data=f"comm:product:{user.id}:{p.id}")] for p in products]
    await callback.message.edit_text(f"Người dùng: {user.display_name}\nVai trò: {user.role.value}\n\nChọn sản phẩm:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()


@router.callback_query(F.data.startswith("comm:product:"))
async def commission_input(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    _, _, raw_user, raw_product = callback.data.split(":"); user_id, product_id = int(raw_user), int(raw_product)
    product = await session.get(Product, product_id); user = await session.get(User, user_id)
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not product or not actor or not can_edit_commission(actor) or not user: return await callback.answer("Chỉ SUPER_ADMIN được sửa hoa hồng.", show_alert=True)
    custom = await session.scalar(select(UserProductCommission).where(UserProductCommission.user_id == user_id, UserProductCommission.product_id == product_id))
    current = product_commission(product) if not custom else format_commission(custom.commission_type, custom.commission_value, percent_is_basis_points=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Tăng", callback_data=f"comm:adjust:{user_id}:{product_id}:plus"),
         InlineKeyboardButton(text="➖ Giảm", callback_data=f"comm:adjust:{user_id}:{product_id}:minus")],
        [InlineKeyboardButton(text="✏️ Nhập trực tiếp", callback_data=f"comm:direct:{user_id}:{product_id}")],
        [InlineKeyboardButton(text="♻️ Về mặc định", callback_data=f"comm:reset:{user_id}:{product_id}")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data=f"comm:user:{user_id}")],
    ])
    await callback.message.edit_text(f"💰 HOA HỒNG\n\nNgười dùng: {user.display_name}\nVai trò: {user.role.value}\n"
                                     f"Sản phẩm: {product.display_name}\nHoa hồng hiện tại: {current}", reply_markup=keyboard); await callback.answer()


@router.callback_query(F.data.startswith("comm:direct:"))
async def commission_direct(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    _, _, raw_user, raw_product = callback.data.split(":")
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not actor or not can_edit_commission(actor): return await deny(callback)
    await state.set_state(AdminState.user_commission)
    await state.set_data({"commission_user": int(raw_user), "commission_product": int(raw_product)})
    await callback.message.answer("Nhập hoa hồng mới (ví dụ 12% hoặc 5000):"); await callback.answer()


@router.callback_query(F.data.regexp(r"^comm:adjust:\d+:\d+:(plus|minus)$"))
async def commission_adjust(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    _, _, raw_user, raw_product, direction = callback.data.split(":")
    user_id, product_id = int(raw_user), int(raw_product)
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    product = await session.get(Product, product_id); user = await session.get(User, user_id)
    if not actor or not can_edit_commission(actor) or not product or not user: return await deny(callback)
    custom = await session.scalar(select(UserProductCommission).where(UserProductCommission.user_id == user_id, UserProductCommission.product_id == product_id))
    kind = custom.commission_type if custom else product.commission_type
    old_value = custom.commission_value if custom else product.commission_value
    step = 100 if kind == CommissionType.PERCENT else 1000
    new_value = max(0, old_value + (step if direction == "plus" else -step))
    if kind == CommissionType.PERCENT and new_value > 10_000: return await callback.answer("Hoa hồng phần trăm không được vượt 100%.", show_alert=True)
    if not custom:
        custom = UserProductCommission(user_id=user_id, product_id=product_id, commission_type=kind,
            commission_value=new_value, created_by_super_admin_id=actor.id); session.add(custom)
    else: custom.commission_value = new_value
    await audit(session, callback.from_user.id, "UPDATE_USER_COMMISSION", "USER_PRODUCT_COMMISSION", user_id,
                {"product_id": product_id, "value": old_value}, {"product_id": product_id, "value": new_value})
    await session.commit()
    await callback.answer("✅ Đã cập nhật. Bấm lại sản phẩm để xem mức mới.", show_alert=True)


@router.callback_query(F.data.startswith("comm:reset:"))
async def commission_reset(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    _, _, raw_user, raw_product = callback.data.split(":"); user_id, product_id = int(raw_user), int(raw_product)
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not actor or not can_edit_commission(actor): return await deny(callback)
    custom = await session.scalar(select(UserProductCommission).where(UserProductCommission.user_id == user_id, UserProductCommission.product_id == product_id))
    old = {} if not custom else {"type": custom.commission_type.value, "value": custom.commission_value}
    if custom: await session.delete(custom)
    await audit(session, callback.from_user.id, "UPDATE_USER_COMMISSION", "USER_PRODUCT_COMMISSION", user_id,
                {"product_id": product_id, **old}, {"product_id": product_id, "default": True})
    await session.commit(); await callback.answer("✅ Đã đưa về hoa hồng mặc định.", show_alert=True)


@router.message(AdminState.user_commission)
async def save_user_commission(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    try: commission_type, commission_value = parse_commission(message.text or "")
    except ValueError as exc: return await message.answer(f"❌ {exc}")
    data = await state.get_data(); user_id, product_id = int(data["commission_user"]), int(data["commission_product"])
    custom = await session.scalar(select(UserProductCommission).where(UserProductCommission.user_id == user_id, UserProductCommission.product_id == product_id))
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    employee = await session.get(User, user_id)
    if not actor or not employee or not can_edit_commission(actor):
        await state.clear(); return await message.answer("❌ Chỉ SUPER_ADMIN được sửa hoa hồng.")
    old = {} if not custom else {"type": custom.commission_type.value, "value": custom.commission_value}
    if not custom:
        custom = UserProductCommission(user_id=user_id, product_id=product_id, created_by_super_admin_id=actor.id,
                                           commission_type=commission_type, commission_value=commission_value); session.add(custom)
    else: custom.commission_type, custom.commission_value = commission_type, commission_value
    await session.flush(); await audit(session, message.from_user.id, "UPDATE_USER_COMMISSION", "USER_PRODUCT_COMMISSION", custom.id, old,
                                       {"type": commission_type.value, "value": commission_value})
    await session.commit(); await state.clear(); await message.answer("✅ Đã cập nhật hoa hồng riêng.", reply_markup=admin_menu(super_admin=message.from_user.id in settings.super_admin_ids))


@router.callback_query(F.data.startswith("roles:list:"))
async def role_list(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try:
        _, _, action, raw_page = callback.data.split(":"); page = int(raw_page)
    except (ValueError, AttributeError):
        return await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
    query = select(User).where(User.is_deleted.is_(False))
    if action == "promote": query = query.where(User.role == UserRole.USER)
    else: query = query.where(User.role == UserRole.ADMIN)
    result = await session.execute(query.order_by(User.id.asc()))
    users = list(result.scalars().all())
    if action == "promote":
        logger.info("Promotable users count=%s users=%s", len(users), [(u.id, u.telegram_id, u.role) for u in users])
    if not users: return await callback.answer("Không có tài khoản phù hợp.", show_alert=True)
    pages = max(1, (len(users) + ADMIN_USER_PAGE_SIZE - 1) // ADMIN_USER_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    current = users[page * ADMIN_USER_PAGE_SIZE:(page + 1) * ADMIN_USER_PAGE_SIZE]
    title = "👑 BỔ NHIỆM ADMIN\n\nChọn nhân viên:" if action == "promote" else "Chọn tài khoản:"
    await callback.message.edit_text(title, reply_markup=admin_role_list_keyboard(current, action, page, pages)); await callback.answer()


@router.callback_query(F.data == "roles:menu")
async def roles_menu(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await callback.message.edit_text("👑 QUẢN LÝ ADMIN", reply_markup=super_admin_keyboard()); await callback.answer()


@router.callback_query(F.data == "roles:noop")
async def roles_noop(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("roles:do:"))
async def role_action(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try: _, _, action, raw_id = callback.data.split(":"); target = await session.get(User, int(raw_id))
    except (ValueError, AttributeError): target = None
    if not target or target.role == UserRole.SUPER_ADMIN: return await callback.answer("Không được thay đổi SUPER_ADMIN.", show_alert=True)
    old = {"role": target.role.value}
    if action == "promote":
        target.role = UserRole.ADMIN; target.promoted_at = datetime.now(); target.manager_admin_id = None
        actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id)); target.promoted_by = actor.id if actor else None
        audit_action = "PROMOTE_ADMIN"
    elif action == "demote":
        await session.execute(User.__table__.update().where(User.manager_admin_id == target.id).values(manager_admin_id=None))
        target.role = UserRole.USER; target.promoted_by = None; target.promoted_at = None; audit_action = "DEMOTE_ADMIN"
    elif action == "admins":
        actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        users = await managed_users(session, actor, target.id)
        totals = await group_totals(session, users)
        paid = await paid_admin_total(session, target.id)
        received = await received_from_staff_total(session, target.id)
        receiver = await get_payment_receiver(session, target)
        due = await amount_due_to_receiver(session, target, receiver) if receiver else 0
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Xem nhân viên", callback_data=f"roles:team:{target.id}")],
            [InlineKeyboardButton(text="💸 Lịch sử nộp tiền", callback_data=f"roles:payments:{target.id}")],
            [InlineKeyboardButton(text="🔄 Reset công nợ về 0", callback_data=f"debtreset:ask:{target.id}")],
            [InlineKeyboardButton(text="📜 Lịch sử reset công nợ", callback_data=f"debtreset:history:{target.id}")],
            [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="roles:list:admins:0")],
        ])
        text = (f"👑 {target.display_name.upper()}\n\nNhân viên: {len(users)}\n"
                f"Doanh thu: {format_money(totals['revenue'])}\n"
                f"Hoa hồng: {format_money(totals['commission'])}\nTiền ship: {format_money(totals['shipping'])}\n"
                f"Đã nhận từ nhân viên: {format_money(received)}\n"
                f"Tiền hiện cần chuyển: {format_money(due)}\n"
                f"Đã chuyển về Admin tổng: {format_money(paid)}")
        await callback.message.edit_text(text, reply_markup=keyboard); return await callback.answer()
    else: return await callback.answer("Thao tác không hợp lệ.", show_alert=True)
    await audit(session, callback.from_user.id, audit_action, "USER", target.id, old,
                {"role": target.role.value})
    await session.commit()
    if action == "demote": object.__setattr__(settings, "admin_ids", settings.admin_ids - {target.telegram_id})
    if action == "promote": object.__setattr__(settings, "admin_ids", settings.admin_ids | {target.telegram_id})
    try: await callback.bot.send_message(target.telegram_id, "👑 TÀI KHOẢN ĐÃ ĐƯỢC NÂNG QUYỀN\n\nBạn đã được bổ nhiệm làm Admin." if action == "promote" else "Quyền tài khoản của bạn đã được cập nhật.")
    except Exception: pass
    back_action = "promote" if action == "promote" else "admins"
    rows = []
    if action == "promote":
        rows.append([InlineKeyboardButton(text="💰 Cấu hình hoa hồng Admin", callback_data=f"comm:user:{target.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Quay lại danh sách", callback_data=f"roles:list:{back_action}:0")])
    notice = "✅ Đã bổ nhiệm Admin. Hoa hồng hiện tại được giữ nguyên; thay đổi mới chỉ áp dụng cho giao dịch tiếp theo." if action == "promote" else "✅ Đã cập nhật quyền tài khoản."
    await callback.message.edit_text(notice, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()


@router.callback_query(F.data == "roles:activity")
async def admin_activity(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    logs = list((await session.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(50))).all())
    text = "📊 HOẠT ĐỘNG ADMIN\n\n" + ("\n".join(f"{x.created_at:%d/%m %H:%M} — {x.action} — {x.target_type} #{x.target_id or '-'}" for x in logs) or "Chưa có hoạt động.")
    await callback.message.edit_text(text[:4000]); await callback.answer()


@router.callback_query(F.data.startswith("roles:team:"))
async def super_admin_team(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    admin_id = int(callback.data.rsplit(":", 1)[1])
    admin = await session.get(User, admin_id)
    if not admin or admin.role != UserRole.ADMIN: return await callback.answer("Admin không hợp lệ.", show_alert=True)
    users = list((await session.scalars(select(User).where(User.role == UserRole.USER, User.manager_admin_id == admin.id, User.is_deleted.is_(False)).order_by(User.first_name))).all())
    rows = [[InlineKeyboardButton(text=user.display_name, callback_data=f"team:user:{user.id}")] for user in users]
    rows.append([InlineKeyboardButton(text="⬅️ Quay lại", callback_data=f"roles:do:admins:{admin.id}")])
    await callback.message.edit_text(f"👥 NHÂN VIÊN CỦA {admin.display_name.upper()}\n\n" + ("Chọn nhân viên:" if users else "Chưa có nhân viên."), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()


@router.callback_query(F.data.startswith("roles:payments:"))
async def super_admin_payments(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    admin_id = int(callback.data.rsplit(":", 1)[1]); admin = await session.get(User, admin_id)
    if not admin: return await callback.answer("Admin không tồn tại.", show_alert=True)
    rows = list((await session.scalars(select(AdminPayment).where(AdminPayment.admin_user_id == admin_id).order_by(AdminPayment.created_at.desc()).limit(30))).all())
    history = "\n".join(f"• {x.created_at:%d/%m %H:%M} — {format_money(x.amount)} — {x.status.value}" for x in rows) or "Chưa có giao dịch."
    await callback.message.edit_text(f"💸 LỊCH SỬ NỘP TIỀN — {admin.display_name}\n\n{history}"[:4000]); await callback.answer()


def product_commission(product: Product) -> str:
    return format_commission(product.commission_type, product.commission_value, percent_is_basis_points=True)


def product_summary(product: Product) -> str:
    return (
        "📦 THÔNG TIN SẢN PHẨM\n\n"
        f"Tên:\n{product.name}\n\nQuy cách:\n{product.display_size or 'Không có'}\n\n"
        f"Đơn vị quản lý:\n{product.quantity_unit}\n\n"
        f"Giá bán:\n{format_money(product.price)}\n\nHoa hồng:\n{product_commission(product)}\n\n"
        f"Trạng thái:\n{'✅ Đang hoạt động' if product.active else '🚫 Đã ẩn'}"
    )


def product_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Thêm sản phẩm", callback_data="prod:add")],
        [InlineKeyboardButton(text="📋 Danh sách sản phẩm", callback_data="prod:browse:list:0")],
        [InlineKeyboardButton(text="✏️ Sửa sản phẩm", callback_data="prod:browse:edit:0")],
        [InlineKeyboardButton(text="🚫 Ẩn sản phẩm", callback_data="prod:browse:hide:0")],
        [InlineKeyboardButton(text="✅ Hiện sản phẩm", callback_data="prod:browse:show:0")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="prod:admin_back")],
    ])


def product_detail_keyboard(product: Product, page: int = 0, mode: str = "edit") -> InlineKeyboardMarkup:
    visibility = ("🚫 Ẩn", "hide") if product.active else ("✅ Hiện", "show")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Sửa tên", callback_data=f"prod:edit:{product.id}:name:{page}"),
         InlineKeyboardButton(text="💵 Sửa giá", callback_data=f"prod:edit:{product.id}:price:{page}")],
        [InlineKeyboardButton(text="📏 Sửa quy cách", callback_data=f"prod:edit:{product.id}:size:{page}"),
         InlineKeyboardButton(text="📦 Sửa đơn vị", callback_data=f"prod:edit:{product.id}:stock_unit:{page}")],
        [InlineKeyboardButton(text="💰 Sửa hoa hồng", callback_data=f"prod:edit:{product.id}:commission:{page}")],
        [InlineKeyboardButton(text=visibility[0], callback_data=f"prod:{visibility[1]}:{product.id}"),
         InlineKeyboardButton(text="🗑 Xóa", callback_data=f"prod:delete:{product.id}")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data=f"prod:browse:{mode}:{page}")],
    ])


def edit_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Xác nhận", callback_data="prod:edit_confirm:yes"),
        InlineKeyboardButton(text="❌ Hủy", callback_data="prod:edit_confirm:no"),
    ]])


@router.message(F.text == "📦 Quản lý sản phẩm")
async def product_management(message: Message, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: return await deny(message)
    await message.answer("📦 QUẢN LÝ SẢN PHẨM", reply_markup=product_management_keyboard())


@router.callback_query(F.data == "prod:add")
async def add_product_start(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await state.set_state(AdminState.product_name)
    await callback.message.answer("Tên sản phẩm:"); await callback.answer()


@router.message(AdminState.product_name)
async def add_product_name(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    name = (message.text or "").strip()
    if not name: return await message.answer("Tên sản phẩm không được để trống.")
    await state.update_data(name=name); await state.set_state(AdminState.product_size)
    await message.answer("Quy cách (ví dụ: 330ml; nhập - nếu không có):")


@router.message(AdminState.product_size)
async def add_product_size(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    try: variant, unit = split_size(message.text or "")
    except ValueError as exc: return await message.answer(str(exc))
    await state.update_data(variant=variant, unit=unit); await state.set_state(AdminState.product_stock_unit)
    await message.answer("Đơn vị quản lý tồn kho (ví dụ: chai, lon, hộp, gói, kg):")


@router.message(AdminState.product_stock_unit)
async def add_product_stock_unit(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    try: stock_unit = validate_stock_unit(message.text or "")
    except ValueError as exc: return await message.answer(str(exc))
    await state.update_data(stock_unit=stock_unit); await state.set_state(AdminState.product_price)
    await message.answer("Giá bán (ví dụ: 12000):")


@router.message(AdminState.product_price)
async def add_product_price(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    try: price = parse_integer(message.text or "", "Giá bán")
    except ValueError as exc: return await message.answer(str(exc))
    await state.update_data(price=price); await state.set_state(AdminState.product_commission)
    await message.answer("Hoa hồng (ví dụ: 10% hoặc 1000):")


@router.message(AdminState.product_commission)
async def add_product_commission(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    try: commission_type, commission_value = parse_commission(message.text or "")
    except ValueError as exc: return await message.answer(str(exc))
    data = await state.get_data()
    product = Product(name=data["name"], variant=data["variant"], unit=data["unit"], stock_unit=data["stock_unit"], price=int(data["price"]),
                      commission_type=commission_type, commission_value=commission_value, active=True)
    session.add(product); await session.flush()
    await audit(session, message.from_user.id, "CREATE_PRODUCT", "PRODUCT", product.id, {}, {"name": product.name})
    await session.commit(); await state.clear()
    await message.answer(f"✅ Đã thêm sản phẩm.\n\n{product_summary(product)}", reply_markup=admin_menu())


def product_page_keyboard(products: list[Product], mode: str, page: int, pages: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if product.active else '🚫'} {product.display_name}",
        callback_data=f"prod:select:{mode}:{product.id}:{page}",
    )] for product in products]
    if pages > 1:
        rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"prod:browse:{mode}:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="prod:noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"prod:browse:{mode}:{min(pages - 1, page + 1)}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Quay lại", callback_data="prod:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "prod:menu")
async def product_menu_callback(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await callback.message.edit_text("📦 QUẢN LÝ SẢN PHẨM", reply_markup=product_management_keyboard())
    await callback.answer()


@router.callback_query(F.data == "prod:admin_back")
async def product_back_admin(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await callback.message.edit_text("Đã quay lại menu Admin.")
    await callback.message.answer("Menu Admin", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "prod:noop")
async def product_noop(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^prod:browse:(list|edit|hide|show):\d+$"))
async def product_browse(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try:
        _, _, mode, raw_page = callback.data.split(":"); page = int(raw_page)
    except (ValueError, AttributeError):
        return await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
    products = await all_products(session)
    if mode == "hide": products = [product for product in products if product.active]
    if mode == "show": products = [product for product in products if not product.active]
    if not products:
        return await callback.answer("Không có sản phẩm phù hợp.", show_alert=True)
    pages = max(1, (len(products) + PRODUCT_PAGE_SIZE - 1) // PRODUCT_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    current = products[page * PRODUCT_PAGE_SIZE:(page + 1) * PRODUCT_PAGE_SIZE]
    titles = {"list": "📋 DANH SÁCH SẢN PHẨM", "edit": "✏️ CHỌN SẢN PHẨM CẦN SỬA",
              "hide": "🚫 CHỌN SẢN PHẨM CẦN ẨN", "show": "✅ CHỌN SẢN PHẨM CẦN HIỆN"}
    await callback.message.edit_text(titles[mode], reply_markup=product_page_keyboard(current, mode, page, pages))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^prod:select:(list|edit|hide|show):\d+:\d+$"))
async def product_select(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try:
        _, _, mode, raw_id, raw_page = callback.data.split(":")
        product_id, page = int(raw_id), int(raw_page)
    except (ValueError, AttributeError):
        return await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
    product = await session.get(Product, product_id)
    if not product: return await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
    if mode in {"hide", "show"}:
        old_active = product.active; product.active = mode == "show"
        await audit(session, callback.from_user.id, "SHOW_PRODUCT" if product.active else "HIDE_PRODUCT", "PRODUCT", product.id,
                    {"active": old_active}, {"active": product.active})
        await session.commit()
        await callback.message.edit_text(
            product_summary(product), reply_markup=product_detail_keyboard(product, page, mode)
        )
        return await callback.answer("Đã cập nhật trạng thái sản phẩm.", show_alert=True)
    await callback.message.edit_text(product_summary(product), reply_markup=product_detail_keyboard(product, page, mode))
    await callback.answer()


@router.callback_query(F.data.startswith("prod:edit:"))
async def product_edit_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try:
        _, _, raw_id, field, raw_page = callback.data.split(":")
        product_id, page = int(raw_id), int(raw_page)
    except (ValueError, AttributeError):
        return await callback.answer("Dữ liệu không hợp lệ.", show_alert=True)
    product = await session.get(Product, product_id)
    labels = {"name": ("Tên", product.name if product else ""),
              "price": ("Giá", format_money(product.price) if product else ""),
              "size": ("Quy cách", product.display_size if product else ""),
              "stock_unit": ("Đơn vị quản lý", product.quantity_unit if product else ""),
              "commission": ("Hoa hồng", product_commission(product) if product else "")}
    if not product: return await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
    if field not in labels: return await callback.answer("Trường sửa không hợp lệ.", show_alert=True)
    label, current = labels[field]
    await state.set_state(AdminState.product_edit)
    await state.set_data({"product_id": product_id, "field": field, "page": page})
    await callback.message.answer(f"{label} hiện tại:\n{current}\n\nNhập {label.lower()} mới:")
    await callback.answer()


@router.message(AdminState.product_edit)
async def product_edit_preview(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    data = await state.get_data(); product = await session.get(Product, int(data.get("product_id", 0)))
    if not product: await state.clear(); return await message.answer("Sản phẩm không tồn tại.")
    field, value = data.get("field"), (message.text or "").strip()
    try:
        if field == "name":
            if not value: raise ValueError("Tên không được để trống.")
            proposal, old_text, new_text = {"name": value}, product.name, value
        elif field == "price":
            parsed = parse_integer(value, "Giá bán")
            proposal, old_text, new_text = {"price": parsed}, format_money(product.price), format_money(parsed)
        elif field == "size":
            variant, unit = split_size(value)
            proposal, old_text, new_text = {"variant": variant, "unit": unit}, product.display_size, value
        elif field == "stock_unit":
            stock_unit = validate_stock_unit(value)
            proposal, old_text, new_text = {"stock_unit": stock_unit}, product.quantity_unit, stock_unit
        elif field == "commission":
            commission_type, commission_value = parse_commission(value)
            proposal = {"commission_type": commission_type.value, "commission_value": commission_value}
            old_text = product_commission(product)
            new_text = format_commission(commission_type, commission_value, percent_is_basis_points=True)
        else:
            raise ValueError("Trường sửa không hợp lệ.")
    except ValueError as exc:
        return await message.answer(f"❌ {exc}")
    warning = ""
    if field == "commission" and proposal["commission_type"] == CommissionType.FIXED_PER_ITEM.value and int(proposal["commission_value"]) >= product.price:
        warning = "\n\n⚠️ Hoa hồng đang lớn hơn hoặc bằng giá bán."
    labels = {"name": "Tên", "price": "Giá", "size": "Quy cách", "stock_unit": "Đơn vị quản lý", "commission": "Hoa hồng"}
    await state.update_data(proposal=proposal); await state.set_state(AdminState.product_edit_confirm)
    await message.answer(
        f"{labels[field]} cũ:\n{old_text}\n\n{labels[field]} mới:\n{new_text}{warning}",
        reply_markup=edit_confirmation_keyboard(),
    )


@router.callback_query(AdminState.product_edit_confirm, F.data.startswith("prod:edit_confirm:"))
async def product_edit_confirm(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(callback)
    data = await state.get_data(); product = await session.get(Product, int(data.get("product_id", 0)))
    if not product: await state.clear(); return await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
    if callback.data.endswith(":yes"):
        proposal = data.get("proposal", {})
        old = {"name": product.name, "variant": product.variant, "unit": product.unit, "stock_unit": product.stock_unit,
               "price": product.price, "commission_type": product.commission_type.value,
               "commission_value": product.commission_value}
        if "name" in proposal: product.name = proposal["name"]
        if "price" in proposal: product.price = int(proposal["price"])
        if "variant" in proposal: product.variant, product.unit = proposal["variant"], proposal.get("unit", "")
        if "stock_unit" in proposal: product.stock_unit = proposal["stock_unit"]
        if "commission_type" in proposal:
            product.commission_type = CommissionType(proposal["commission_type"])
            product.commission_value = int(proposal["commission_value"])
        action = "UPDATE_PRICE" if "price" in proposal else "UPDATE_COMMISSION" if "commission_type" in proposal else "UPDATE_PRODUCT"
        await audit(session, callback.from_user.id, action, "PRODUCT", product.id, old, proposal)
        await session.commit(); notice = "✅ Đã cập nhật sản phẩm.\n\n"
    else:
        notice = "❌ Đã hủy thay đổi.\n\n"
    page = int(data.get("page", 0)); await state.clear()
    await callback.message.edit_text(notice + product_summary(product), reply_markup=product_detail_keyboard(product, page, "edit"))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^prod:(hide|show|delete):\d+$"))
async def product_status_action(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    _, action, raw_id = callback.data.split(":"); product = await session.get(Product, int(raw_id))
    if not product: return await callback.answer("Sản phẩm không tồn tại.", show_alert=True)
    if action == "delete":
        deleted = await delete_or_hide_product(session, product)
        text = ("✅ Đã xóa vĩnh viễn sản phẩm chưa từng được sử dụng."
                if deleted else "⚠️ Sản phẩm đã có lịch sử giao dịch nên không thể xóa vĩnh viễn.\n\nSản phẩm đã được chuyển sang trạng thái Ẩn.")
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Quay lại", callback_data="prod:menu")
        ]]))
        return await callback.answer()
    old_active = product.active; product.active = action == "show"
    await audit(session, callback.from_user.id, "SHOW_PRODUCT" if product.active else "HIDE_PRODUCT", "PRODUCT", product.id,
                {"active": old_active}, {"active": product.active})
    await session.commit()
    await callback.message.edit_text(product_summary(product), reply_markup=product_detail_keyboard(product))
    await callback.answer()


@router.message(F.text == "👥 Danh sách người dùng")
async def list_users(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids:
        return await deny(message)
    users = await all_users(session)
    if message.from_user.id not in settings.super_admin_ids:
        users = [user for user in users if user.role == UserRole.USER]
    if not users:
        return await message.answer("Chưa có người dùng nào từng sử dụng bot.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{user.display_name} ({user.telegram_id})", callback_data=f"admin:info:{user.id}")
    ] for user in users])
    await message.answer("👥 NGƯỜI DÙNG\n\nChọn tài khoản để xem:", reply_markup=keyboard)


@router.message(F.text == "🔎 Tìm người dùng")
async def search_start(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids:
        return await deny(message)
    await state.set_state(AdminState.searching_user)
    await message.answer("Nhập Telegram ID, username hoặc tên người dùng cần tìm:")


@router.message(AdminState.searching_user)
async def search_result(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids:
        await state.clear()
        return await deny(message)
    term = (message.text or "").strip().lstrip("@")
    if not term:
        return await message.answer("Nội dung tìm kiếm không được để trống.")
    pattern = f"%{term}%"
    users = list((await session.scalars(
        select(User).where(User.is_deleted.is_(False), or_(
            cast(User.telegram_id, String).like(pattern),
            User.username.ilike(pattern), User.first_name.ilike(pattern),
            User.last_name.ilike(pattern),
        )).order_by(User.created_at.desc()).limit(20)
    )).all())
    await state.clear()
    if not users:
        return await message.answer("Không tìm thấy người dùng phù hợp.", reply_markup=admin_menu())
    await message.answer(
        "🔎 KẾT QUẢ\n\n" + "\n\n━━━━━━━━━━━━\n\n".join(user_summary(user) for user in users),
        reply_markup=admin_menu(),
    )


async def show_action_list(
    message: Message, session: AsyncSession, settings: Settings, action: str, title: str
) -> None:
    if message.from_user.id not in settings.super_admin_ids:
        return await deny(message)
    users = await all_users(session)
    if message.from_user.id not in settings.super_admin_ids:
        users = [user for user in users if user.role == UserRole.USER]
    if not users:
        return await message.answer("Không có người dùng phù hợp.")
    role_icons = {UserRole.SUPER_ADMIN: "👑", UserRole.ADMIN: "🛡️", UserRole.USER: "👤"}
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"{role_icons[user.role]} {user.display_name} ({user.telegram_id})",
            callback_data=f"admin:{action}:{user.id}",
        )
    ] for user in users])
    await message.answer(title, reply_markup=keyboard)


@router.message(F.text == "📊 Xem thông tin người dùng")
async def info_list(message: Message, session: AsyncSession, settings: Settings) -> None:
    await show_action_list(message, session, settings, "info", "Chọn người dùng cần xem:")


@router.message(F.text.in_({"👥 Nhân viên", "👥 Nhân viên của tôi", "👥 Tất cả nhân viên"}))
async def employees_list(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings): return await deny(message)
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    if not actor: return await deny(message)
    users = await managed_users(session, actor)
    if not users: return await message.answer("Chưa có nhân viên thuộc nhóm của bạn.")
    rows = []
    for user in users:
        rows.append([InlineKeyboardButton(text=user.display_name, callback_data=f"team:user:{user.id}")])
    await message.answer("👥 NHÂN VIÊN\n\nChọn nhân viên để xem chi tiết:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(F.text == "🔗 Mời nhân viên")
async def invite_employee(message: Message, session: AsyncSession, settings: Settings) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    if not actor or actor.role != UserRole.ADMIN or actor.is_deleted: return await deny(message)
    bot_info = await message.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=invite_{actor.id}"
    await message.answer("🔗 LINK MỜI NHÂN VIÊN\n\nGửi link này cho nhân viên:\n" + link +
                         "\n\nNhân viên đã thuộc Admin khác sẽ không bị tự động chuyển nhóm.")


@router.message(F.text == "📊 Tổng hợp nhóm")
async def team_summary(message: Message, session: AsyncSession, settings: Settings) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    if not actor or actor.role != UserRole.ADMIN or actor.is_deleted: return await deny(message)
    users = await managed_users(session, actor)
    team = await group_totals(session, users)
    own = await employee_totals(session, actor.id)
    await message.answer(
        "📊 TỔNG HỢP NHÓM\n\n"
        f"👤 NHÓM NHÂN VIÊN ({len(users)})\nDoanh thu: {format_money(team['revenue'])}\n"
        f"Hoa hồng: {format_money(team['commission'])}\nTiền ship: {format_money(team['shipping'])}\n"
        f"Tiền còn lại: {format_money(team['company'])}\n\n"
        f"👑 CÁ NHÂN ADMIN\nDoanh thu: {format_money(own['revenue'])}\n"
        f"Hoa hồng: {format_money(own['commission'])}\nTiền ship: {format_money(own['shipping'])}\n"
        f"Tiền còn lại: {format_money(own['company'])}\n\n"
        "━━━━━━━━━━━━\n"
        f"Tổng doanh thu: {format_money(team['revenue'] + own['revenue'])}\n"
        f"Tổng hoa hồng: {format_money(team['commission'] + own['commission'])}\n"
        f"Trả Admin tổng: {format_money(team['company'] + own['company'])}"
    )


@router.message(F.text == "📦 Hàng hóa nhóm")
async def team_inventory(message: Message, session: AsyncSession, settings: Settings) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    if not actor or actor.role != UserRole.ADMIN or actor.is_deleted: return await deny(message)
    users = await managed_users(session, actor)
    lines = []
    for user in users:
        rows = list((await session.execute(
            select(Inventory, Product).join(Product, Product.id == Inventory.product_id)
            .where(Inventory.user_id == user.id, Inventory.current_quantity > 0)
            .order_by(Product.name)
        )).all())
        lines.append(f"👤 {user.display_name}")
        lines.extend(
            f"• {product.display_name}: {item.current_quantity} {product.quantity_unit}"
            for item, product in rows
        )
        if not rows: lines.append("• Không có hàng tồn.")
    await message.answer("📦 HÀNG HÓA NHÓM\n\n" + ("\n".join(lines) or "Chưa có nhân viên."))


@router.callback_query(F.data.startswith("team:user:"))
async def team_user_detail(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings): return await deny(callback)
    try: user = await session.get(User, int(callback.data.rsplit(":", 1)[1]))
    except (ValueError, AttributeError): user = None
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not actor or not user or not can_manage_employee(actor, user):
        return await callback.answer("Bạn không quản lý nhân viên này.", show_alert=True)
    t = await employee_totals(session, user.id)
    text = (f"👤 {user.display_name.upper()}\n\nDoanh thu: {format_money(t['revenue'])}\n"
            f"Hoa hồng: {format_money(t['commission'])}\nTiền ship: {format_money(t['shipping'])}\n"
            f"Phải nộp gốc: {format_money(t['company'])}\nĐã nộp: {format_money(t['paid'])}\n"
            f"Đã reset công nợ: {format_money(t['adjusted'])}\nCòn phải nộp: {format_money(t['outstanding'])}")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Chi tiết hàng", callback_data=f"team:stock:{user.id}")],
        [InlineKeyboardButton(text="💰 Xem hoa hồng", callback_data=f"comm:user:{user.id}")],
        [InlineKeyboardButton(text="📜 Lịch sử theo ngày", callback_data=f"hist:m:{user.id}:t")],
        [InlineKeyboardButton(text="🔄 Reset công nợ về 0", callback_data=f"debtreset:ask:{user.id}")],
        [InlineKeyboardButton(text="📜 Lịch sử reset công nợ", callback_data=f"debtreset:history:{user.id}")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard); await callback.answer()


async def authorized_team_user(callback: CallbackQuery, session: AsyncSession, user_id: int) -> User | None:
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    employee = await session.get(User, user_id)
    return employee if actor and employee and can_manage_employee(actor, employee) else None


def debt_reset_back_callback(actor: User, target: User) -> str:
    if actor.role == UserRole.SUPER_ADMIN and target.role == UserRole.ADMIN:
        return f"roles:do:admins:{target.id}"
    return f"team:user:{target.id}"


async def authorized_debt_reset(
    callback: CallbackQuery, session: AsyncSession, target_id: int
) -> tuple[User, User, User] | None:
    actor = await session.scalar(select(User).where(
        User.telegram_id == callback.from_user.id,
        User.is_deleted.is_(False),
    ))
    target = await session.get(User, target_id)
    if not can_reset_debt(actor, target):
        return None
    receiver = await get_payment_receiver(session, target)
    if receiver is None:
        return None
    return actor, target, receiver


@router.callback_query(F.data.regexp(r"^debtreset:ask:\d+$"))
async def debt_reset_ask(callback: CallbackQuery, session: AsyncSession) -> None:
    target_id = int(callback.data.rsplit(":", 1)[1])
    authorized = await authorized_debt_reset(callback, session, target_id)
    if not authorized:
        return await callback.answer("Bạn không có quyền reset công nợ tài khoản này.", show_alert=True)
    actor, target, receiver = authorized
    debt = await debt_summary_to_receiver(session, target, receiver)
    back = debt_reset_back_callback(actor, target)
    if debt.outstanding <= 0:
        await callback.message.edit_text(
            f"✅ Công nợ của {target.display_name} hiện đã là 0đ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Quay lại", callback_data=back)
            ]]),
        )
        return await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Xác nhận reset", callback_data=f"debtreset:confirm:{target.id}")],
        [InlineKeyboardButton(text="❌ Hủy", callback_data=f"debtreset:cancel:{target.id}")],
    ])
    await callback.message.edit_text(
        "⚠️ XÁC NHẬN RESET CÔNG NỢ\n\n"
        f"Nhân viên:\n{target.display_name}\n\n"
        f"Còn phải nộp hiện tại:\n{format_money(debt.outstanding)}\n\n"
        "Sau khi reset:\n0đ\n\n"
        "Thao tác này không xóa lịch sử doanh thu và thanh toán.",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^debtreset:confirm:\d+$"))
async def debt_reset_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    target_id = int(callback.data.rsplit(":", 1)[1])
    authorized = await authorized_debt_reset(callback, session, target_id)
    if not authorized:
        return await callback.answer("Bạn không có quyền reset công nợ tài khoản này.", show_alert=True)
    actor, target, _receiver = authorized
    try:
        result = await reset_debt(session, actor, target)
    except (PermissionError, ValueError) as exc:
        return await callback.answer(str(exc), show_alert=True)
    back = debt_reset_back_callback(actor, target)
    if not result.created:
        await callback.message.edit_text(
            f"✅ Công nợ của {target.display_name} hiện đã là 0đ.\n\nKhông tạo thêm bút toán điều chỉnh.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Quay lại", callback_data=back)
            ]]),
        )
        return await callback.answer("Công nợ đã bằng 0đ.")
    await callback.message.edit_text(
        f"✅ ĐÃ RESET CÔNG NỢ\n\nNhân viên: {target.display_name}\n"
        f"Số tiền đã điều chỉnh: {format_money(result.amount)}\nCòn phải nộp: 0đ\n\n"
        "Doanh thu, hoa hồng, tiền ship và lịch sử thanh toán vẫn được giữ nguyên.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📜 Lịch sử reset", callback_data=f"debtreset:history:{target.id}")],
            [InlineKeyboardButton(text="⬅️ Quay lại", callback_data=back)],
        ]),
    )
    await callback.answer("Đã reset công nợ về 0đ.")


@router.callback_query(F.data.regexp(r"^debtreset:cancel:\d+$"))
async def debt_reset_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    target_id = int(callback.data.rsplit(":", 1)[1])
    authorized = await authorized_debt_reset(callback, session, target_id)
    if not authorized:
        return await callback.answer("Bạn không có quyền thao tác tài khoản này.", show_alert=True)
    actor, target, _receiver = authorized
    await callback.message.edit_text(
        "Đã hủy reset công nợ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Quay lại", callback_data=debt_reset_back_callback(actor, target))
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^debtreset:history:\d+$"))
async def debt_reset_history(callback: CallbackQuery, session: AsyncSession) -> None:
    target_id = int(callback.data.rsplit(":", 1)[1])
    authorized = await authorized_debt_reset(callback, session, target_id)
    if not authorized:
        return await callback.answer("Bạn không có quyền xem lịch sử này.", show_alert=True)
    actor, target, _receiver = authorized
    rows = await reset_history(session, target.id)
    lines: list[str] = []
    for item in rows:
        creator = await session.get(User, item.created_by_user_id)
        lines.append(
            f"• {item.created_at:%d/%m/%Y %H:%M}\n"
            f"  {creator.display_name if creator else 'Không xác định'} đã reset {format_money(item.amount)}"
        )
    text = "\n\n".join(lines) or "Chưa có lần reset công nợ nào."
    await callback.message.edit_text(
        f"📜 LỊCH SỬ RESET CÔNG NỢ\n\nNhân viên: {target.display_name}\n\n{text}"[:4000],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Quay lại", callback_data=debt_reset_back_callback(actor, target))
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:stock:"))
async def team_stock(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings): return await deny(callback)
    user_id = int(callback.data.rsplit(":", 1)[1]); user = await authorized_team_user(callback, session, user_id)
    if not user: return await callback.answer("Bạn không quản lý nhân viên này.", show_alert=True)
    rows = list((await session.execute(select(Inventory, Product).join(Product, Product.id == Inventory.product_id)
                                      .where(Inventory.user_id == user.id).order_by(Product.name))).all())
    text = "\n".join(
        f"• {product.display_name}: {item.current_quantity} {product.quantity_unit}"
        for item, product in rows
    ) or "Chưa có hàng tồn."
    await callback.message.edit_text(f"📦 CHI TIẾT HÀNG — {user.display_name}\n\n{text}"[:4000]); await callback.answer()


@router.callback_query(F.data.startswith("team:history:"))
async def team_history(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings): return await deny(callback)
    user_id = int(callback.data.rsplit(":", 1)[1]); user = await authorized_team_user(callback, session, user_id)
    if not user: return await callback.answer("Bạn không quản lý nhân viên này.", show_alert=True)
    rows = list((await session.execute(
        select(StockTransaction, Product).join(Product, Product.id == StockTransaction.product_id)
        .where(StockTransaction.user_id == user.id)
        .order_by(StockTransaction.created_at.desc()).limit(50)
    )).all())
    text = "\n".join(
        f"• {item.created_at:%d/%m %H:%M} — {item.transaction_type.value} — "
        f"{item.product_name_snapshot or product.display_name} — {item.quantity:+d} "
        f"{item.product_stock_unit_snapshot or product.stock_unit or 'sản phẩm'}"
        for item, product in rows
    ) or "Chưa có lịch sử."
    await callback.message.edit_text(f"📜 LỊCH SỬ — {user.display_name}\n\n{text}"[:4000]); await callback.answer()


@router.message(F.text == "💸 Nộp tiền Admin tổng")
async def admin_payment_preview(message: Message, session: AsyncSession, settings: Settings) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    if not actor or actor.role != UserRole.ADMIN or actor.is_deleted: return await deny(message)
    receiver = await get_payment_receiver(session, actor)
    if not receiver: return await message.answer("Không tìm thấy Admin tổng nhận tiền.")
    day = vietnam_today(); summary = await daily_settlement(session, actor, receiver, day)
    debt = await debt_summary_to_receiver(session, actor, receiver)
    personal_due = max(0, summary.revenue - summary.commission - summary.shipping)
    staff_received = max(0, summary.due - personal_due)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Nộp 10.000đ", callback_data="adminpay:create:10000"),
         InlineKeyboardButton(text="💳 Nộp 100.000đ", callback_data="adminpay:create:100000")],
        [InlineKeyboardButton(text="💳 Nộp toàn bộ còn lại", callback_data="adminpay:create:all")],
        [InlineKeyboardButton(text="📜 Lịch sử nộp hôm nay", callback_data="settlement:history")],
        [InlineKeyboardButton(text="❌ Hủy", callback_data="adminpay:cancel")],
    ])
    if debt.outstanding == 0:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📜 Lịch sử nộp hôm nay", callback_data="settlement:history")
        ]])
    complete = "\n\n✅ ĐÃ HOÀN TẤT QUYẾT TOÁN" if debt.outstanding == 0 else ""
    await message.answer("💸 NỘP TIỀN ADMIN TỔNG\n\n"
        f"📅 Ngày {day:%d/%m/%Y}\n\nDoanh thu cá nhân: {format_money(summary.revenue)}\n"
        f"Hoa hồng: {format_money(summary.commission)}\nTiền ship: {format_money(summary.shipping)}\n"
        f"Tiền cá nhân phải nộp: {format_money(personal_due)}\n"
        f"Tiền đã thu từ nhân viên: {format_money(staff_received)}\n\n"
        f"Đã nộp Admin tổng: {format_money(debt.paid)}\n"
        f"Đã reset công nợ: {format_money(debt.adjusted)}\n"
        f"Còn lại: {format_money(debt.outstanding)}{complete}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("adminpay:create:"))
async def admin_payment_create(callback: CallbackQuery, session: AsyncSession, settings: Settings, payos: AsyncPayOS) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not actor or actor.role != UserRole.ADMIN or actor.is_deleted: return await deny(callback)
    if not payos_is_configured(settings): return await callback.answer("payOS chưa được cấu hình.", show_alert=True)
    receiver = await get_payment_receiver(session, actor)
    if not receiver: return await callback.answer("Không tìm thấy Admin tổng.", show_alert=True)
    day = vietnam_today(); debt = await debt_summary_to_receiver(session, actor, receiver)
    raw_amount = callback.data.rsplit(":", 1)[1]
    due = debt.outstanding if raw_amount == "all" else min(debt.outstanding, int(raw_amount))
    try:
        item = await create_admin_payment(session, actor.id, due, receiver.id)
        item.settlement_date = day; await session.commit()
        link = await create_admin_payment_link(payos, settings, item)
        item = await mark_admin_link_created(session, item.id, link)
    except ValueError as exc:
        return await callback.answer(str(exc), show_alert=True)
    except Exception:
        logger.exception("Không tạo được link payOS nộp tiền Admin")
        return await callback.answer("Không thể kết nối payOS. Vui lòng thử lại.", show_alert=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 THANH TOÁN", url=item.checkout_url)]])
    await callback.message.edit_text(f"💸 NỘP TIỀN ADMIN TỔNG\n\nSố tiền:\n{format_money(item.amount)}\n\nMã đơn: {item.order_code}", reply_markup=keyboard)
    await audit(session, callback.from_user.id, "ADMIN_PAYMENT_CREATED", "ADMIN_PAYMENT", item.id, {}, {"amount": item.amount})
    await session.commit(); await callback.answer()


@router.callback_query(F.data == "adminpay:cancel")
async def admin_payment_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Đã hủy thao tác nộp tiền."); await callback.answer()


@router.message(F.text.startswith("💸 Nộp tiền"))
async def user_settlement_preview(message: Message, session: AsyncSession, settings: Settings) -> None:
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id, User.is_deleted.is_(False)))
    if not actor or actor.role == UserRole.SUPER_ADMIN: return await deny(message)
    # ADMIN dùng handler chuyên biệt phía trên.
    if actor.role == UserRole.ADMIN: return await admin_payment_preview(message, session, settings)
    receiver = await get_payment_receiver(session, actor)
    if not receiver: return await message.answer("Không tìm thấy người nhận tiền.")
    day = vietnam_today(); summary = await daily_settlement(session, actor, receiver, day)
    debt = await debt_summary_to_receiver(session, actor, receiver)
    if actor.manager_admin_id:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💸 Nộp tiền cho {receiver.display_name}", callback_data="settlement:manual:create")
        ]])
        receiver_label = receiver.display_name
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Nộp 10.000đ", callback_data="settlement:payos:create:10000"),
             InlineKeyboardButton(text="💳 Nộp 100.000đ", callback_data="settlement:payos:create:100000")],
            [InlineKeyboardButton(text="💳 Nộp toàn bộ còn lại", callback_data="settlement:payos:create:all")],
            [InlineKeyboardButton(text="📜 Lịch sử nộp hôm nay", callback_data="settlement:history")],
        ])
        receiver_label = "Admin tổng"
    if debt.outstanding == 0:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📜 Lịch sử nộp hôm nay", callback_data="settlement:history")
        ]])
    await message.answer(f"💰 QUYẾT TOÁN NGÀY {day:%d/%m/%Y}\n\n"
                         f"Doanh thu: {format_money(summary.revenue)}\nHoa hồng: {format_money(summary.commission)}\n\n"
                         f"Tiền ship: {format_money(summary.shipping)}\n"
                         f"Phải nộp: {format_money(summary.due)}\nĐã nộp: {format_money(debt.paid)}\n"
                         f"Đã reset công nợ: {format_money(debt.adjusted)}\n"
                         f"Còn lại: {format_money(debt.outstanding)}\n"
                         f"Người nhận: {receiver_label}" + ("\n\n✅ ĐÃ HOÀN TẤT QUYẾT TOÁN" if debt.outstanding == 0 else ""), reply_markup=keyboard)


@router.callback_query(F.data == "settlement:manual:create")
async def create_manual_settlement(callback: CallbackQuery, session: AsyncSession) -> None:
    payer = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id, User.is_deleted.is_(False)))
    if not payer or payer.role != UserRole.USER or payer.manager_admin_id is None: return await deny(callback)
    receiver = await get_payment_receiver(session, payer)
    if not receiver or receiver.id != payer.manager_admin_id: return await callback.answer("Người nhận không hợp lệ.", show_alert=True)
    existing = await session.scalar(select(AdminPayment).where(
        AdminPayment.payer_user_id == payer.id, AdminPayment.receiver_user_id == receiver.id,
        AdminPayment.payment_method == "MANUAL", AdminPayment.status == AdminPaymentStatus.PENDING,
    ).order_by(AdminPayment.created_at.desc()))
    if existing:
        return await callback.answer("Giao dịch đang chờ quản lý xác nhận.", show_alert=True)
    debt = await debt_summary_to_receiver(session, payer, receiver); due = debt.outstanding
    try: item = await create_settlement(session, payer.id, receiver.id, due, payment_method="MANUAL")
    except ValueError as exc: return await callback.answer(str(exc), show_alert=True)
    await callback.message.edit_text("⏳ CHỜ THANH TOÁN\n\n"
        f"Số tiền: {format_money(item.amount)}\nNgười nhận: {receiver.display_name}\n\n"
        "Sau khi bạn thanh toán trực tiếp, quản lý sẽ xác nhận đã nhận tiền.")
    try: await callback.bot.send_message(receiver.telegram_id, "💰 NHÂN VIÊN BÁO NỘP TIỀN\n\n"
        f"Nhân viên: {payer.display_name}\nSố tiền: {format_money(item.amount)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Xác nhận đã nhận tiền", callback_data=f"settlement:confirm:{item.id}")
        ]]))
    except Exception: logger.exception("Không thông báo được settlement cho Admin")
    await callback.answer()


@router.callback_query(F.data.startswith("settlement:payos:create:"))
async def create_user_payos_settlement(callback: CallbackQuery, session: AsyncSession, settings: Settings, payos: AsyncPayOS) -> None:
    payer = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id, User.is_deleted.is_(False)))
    if not payer or payer.role != UserRole.USER or payer.manager_admin_id is not None: return await deny(callback)
    receiver = await get_payment_receiver(session, payer)
    if not receiver: return await callback.answer("Không tìm thấy Admin tổng.", show_alert=True)
    day = vietnam_today(); debt = await debt_summary_to_receiver(session, payer, receiver)
    raw_amount = callback.data.rsplit(":", 1)[1]
    due = debt.outstanding if raw_amount == "all" else min(debt.outstanding, int(raw_amount))
    try:
        item = await create_settlement(session, payer.id, receiver.id, due, payment_method="PAYOS")
        link = await create_admin_payment_link(payos, settings, item)
        item = await mark_admin_link_created(session, item.id, link)
    except ValueError as exc: return await callback.answer(str(exc), show_alert=True)
    except Exception:
        logger.exception("Không tạo được link payOS settlement")
        return await callback.answer("Không thể kết nối payOS.", show_alert=True)
    await callback.message.edit_text(f"💸 NỘP TIỀN ADMIN TỔNG\n\nSố tiền: {format_money(item.amount)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 THANH TOÁN", url=item.checkout_url)]]))
    await callback.answer()


@router.callback_query(F.data == "settlement:history")
async def settlement_history(callback: CallbackQuery, session: AsyncSession) -> None:
    payer = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id, User.is_deleted.is_(False)))
    if not payer: return await deny(callback)
    day = vietnam_today()
    rows = list((await session.scalars(select(AdminPayment).where(
        AdminPayment.payer_user_id == payer.id, AdminPayment.settlement_date == day,
    ).order_by(AdminPayment.created_at))).all())
    lines = [f"📜 LỊCH SỬ NỘP TIỀN\n{day:%d/%m/%Y}"]
    for item in rows:
        lines.append(f"\n{item.created_at:%H:%M}\n{format_money(item.amount)}\n{item.status.value}\nReference: {item.payos_reference or 'Chưa có'}")
    total = sum(item.amount for item in rows if item.status == AdminPaymentStatus.PAID)
    lines.append(f"\nTổng: {format_money(total)}")
    await callback.message.edit_text("\n".join(lines)); await callback.answer()


@router.message(F.text == "💰 Nhân viên nộp cho tôi")
async def staff_settlements(message: Message, session: AsyncSession, settings: Settings) -> None:
    admin = await session.scalar(select(User).where(User.telegram_id == message.from_user.id, User.role == UserRole.ADMIN, User.is_deleted.is_(False)))
    if not admin: return await deny(message)
    staff = await managed_users(session, admin)
    lines, buttons = [], []
    day = vietnam_today()
    for user in staff:
        summary = await daily_settlement(session, user, admin, day)
        debt = await debt_summary_to_receiver(session, user, admin)
        lines.append(f"{user.display_name}\nTiền ship: {format_money(summary.shipping)}\n"
                     f"Phải nộp: {format_money(summary.due)}\n"
                     f"Đã nộp: {format_money(debt.paid)}\n"
                     f"Đã reset: {format_money(debt.adjusted)}\nCòn: {format_money(debt.outstanding)}")
    pending = list((await session.scalars(select(AdminPayment).where(
        AdminPayment.receiver_user_id == admin.id, AdminPayment.payment_method == "MANUAL",
        AdminPayment.status == AdminPaymentStatus.PENDING,
    ).order_by(AdminPayment.created_at))).all())
    for item in pending:
        payer = await session.get(User, item.payer_user_id)
        if payer and payer.manager_admin_id == admin.id:
            buttons.append([InlineKeyboardButton(text=f"✅ Xác nhận {payer.display_name} · {format_money(item.amount)}",
                                                 callback_data=f"settlement:confirm:{item.id}")])
    total_received = int(await session.scalar(select(func.coalesce(func.sum(AdminPayment.amount), 0)).where(
        AdminPayment.receiver_user_id == admin.id, AdminPayment.status == AdminPaymentStatus.PAID,
        AdminPayment.settlement_date == day)) or 0)
    await message.answer(f"📊 QUYẾT TOÁN NGÀY {day:%d/%m/%Y}\n\n" + ("\n\n".join(lines) or "Chưa có nhân viên.") +
                         f"\n\nTổng đã nhận: {format_money(total_received)}",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)


@router.callback_query(F.data.startswith("settlement:confirm:"))
async def confirm_staff_settlement(callback: CallbackQuery, session: AsyncSession) -> None:
    admin = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id, User.role == UserRole.ADMIN, User.is_deleted.is_(False)))
    if not admin: return await deny(callback)
    try: item = await confirm_manual_settlement(session, admin, int(callback.data.rsplit(":", 1)[1]))
    except PermissionError as exc: return await callback.answer(str(exc), show_alert=True)
    except ValueError as exc: return await callback.answer(str(exc), show_alert=True)
    payer = await session.get(User, item.payer_user_id)
    if payer:
        try: await callback.bot.send_message(payer.telegram_id, f"✅ Quản lý {admin.display_name} đã xác nhận nhận {format_money(item.amount)}.")
        except Exception: pass
    await callback.message.edit_text(f"✅ Đã xác nhận nhận {format_money(item.amount)} từ {payer.display_name if payer else 'nhân viên'}.")
    await callback.answer()


@router.message(F.text == "👤 Chế độ cá nhân")
async def personal_mode(message: Message) -> None:
    from app.keyboards.user import user_menu
    await message.answer("Đã chuyển sang chế độ cá nhân.", reply_markup=user_menu())


async def reminder_menu_text(session: AsyncSession):
    rows = list((await session.scalars(select(PaymentReminderSchedule).where(
        PaymentReminderSchedule.is_active.is_(True)).order_by(PaymentReminderSchedule.hour, PaymentReminderSchedule.minute))).all())
    text = "⏰ GIỜ NHẮC HIỆN TẠI\n\n" + ("\n".join(f"• {x.hour:02d}:{x.minute:02d}" for x in rows) or "Chưa đặt giờ nhắc.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Thêm giờ nhắc", callback_data="reminder:add")],
        [InlineKeyboardButton(text="🗑 Xóa giờ nhắc", callback_data="reminder:delete:list")],
        [InlineKeyboardButton(text="📋 Xem lịch", callback_data="reminder:view")],
    ])
    return text, keyboard


@router.message(F.text == "⏰ Nhắc nộp tiền")
async def reminder_menu(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: return await deny(message)
    text, keyboard = await reminder_menu_text(session); await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "reminder:add")
async def reminder_add(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    await state.set_state(AdminState.reminder_time); await callback.message.answer("Nhập giờ nhắc dạng HH:MM, ví dụ 17:00:"); await callback.answer()


@router.message(AdminState.reminder_time)
async def reminder_save(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: await state.clear(); return await deny(message)
    try:
        hour, minute = (int(x) for x in (message.text or "").strip().split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59: raise ValueError
    except (ValueError, TypeError): return await message.answer("❌ Giờ không hợp lệ. Hãy nhập HH:MM, ví dụ 17:00.")
    actor = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
    existing = await session.scalar(select(PaymentReminderSchedule).where(PaymentReminderSchedule.hour == hour, PaymentReminderSchedule.minute == minute))
    if existing: existing.is_active = True
    else: session.add(PaymentReminderSchedule(hour=hour, minute=minute, created_by_user_id=actor.id))
    await session.commit(); await state.clear(); await message.answer(f"✅ Đã lưu giờ nhắc {hour:02d}:{minute:02d}.")


@router.callback_query(F.data == "reminder:delete:list")
async def reminder_delete_list(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    rows = list((await session.scalars(select(PaymentReminderSchedule).where(PaymentReminderSchedule.is_active.is_(True)).order_by(PaymentReminderSchedule.hour, PaymentReminderSchedule.minute))).all())
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🗑 {x.hour:02d}:{x.minute:02d}", callback_data=f"reminder:delete:{x.id}")] for x in rows])
    await callback.message.edit_text("Chọn giờ nhắc cần xóa:", reply_markup=keyboard); await callback.answer()


@router.callback_query(F.data.regexp(r"^reminder:delete:\d+$"))
async def reminder_delete(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    row = await session.get(PaymentReminderSchedule, int(callback.data.rsplit(":", 1)[1]))
    if row: row.is_active = False; await session.commit()
    await callback.message.edit_text("✅ Đã xóa giờ nhắc."); await callback.answer()


@router.callback_query(F.data == "reminder:view")
async def reminder_view(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    text, keyboard = await reminder_menu_text(session); await callback.message.edit_text(text, reply_markup=keyboard); await callback.answer()


@router.callback_query(F.data.startswith("admin:"))
async def user_action(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids:
        return await deny(callback)
    try:
        _, action, raw_id = callback.data.split(":", 2)
        record_id = int(raw_id)
    except (ValueError, AttributeError):
        return await callback.answer("Dữ liệu thao tác không hợp lệ.", show_alert=True)
    user = await session.get(User, record_id)
    if user is None:
        return await callback.answer("Người dùng không còn tồn tại.", show_alert=True)
    if callback.from_user.id not in settings.super_admin_ids and user.role != UserRole.USER:
        return await callback.answer("❌ Bạn không có quyền thực hiện thao tác này.", show_alert=True)
    if action == "info":
        text = await user_details(user, session)
        rows = [[InlineKeyboardButton(text="📜 Lịch sử theo ngày", callback_data=f"hist:m:{user.id}:a")]]
        if user.role != UserRole.SUPER_ADMIN and not user.is_deleted:
            rows.append([InlineKeyboardButton(text="🔄 Reset công nợ về 0", callback_data=f"debtreset:ask:{user.id}")])
            rows.append([InlineKeyboardButton(text="📜 Lịch sử reset công nợ", callback_data=f"debtreset:history:{user.id}")])
            rows.append([InlineKeyboardButton(text="🗑 Xóa người dùng", callback_data=f"softdelete:ask:{user.id}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.edit_text(text, reply_markup=keyboard)
        return await callback.answer()
    else:
        return await callback.answer("Thao tác không hợp lệ.", show_alert=True)
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data.startswith("softdelete:ask:"))
async def soft_delete_ask(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try: target = await session.get(User, int(callback.data.rsplit(":", 1)[1]))
    except (ValueError, AttributeError): target = None
    if not target: return await callback.answer("Người dùng không tồn tại.", show_alert=True)
    if target.role == UserRole.SUPER_ADMIN: return await callback.answer("❌ Không thể xóa Admin chính.", show_alert=True)
    if target.is_deleted: return await callback.answer("Tài khoản này đã được xóa trước đó.", show_alert=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 XÁC NHẬN XÓA", callback_data=f"softdelete:confirm:{target.id}")],
        [InlineKeyboardButton(text="❌ Hủy", callback_data=f"admin:info:{target.id}")],
    ])
    await callback.message.edit_text("⚠️ XÁC NHẬN XÓA NGƯỜI DÙNG\n\n"
        f"Tên:\n{target.display_name}\n\nTelegram ID:\n{target.telegram_id}\n\nVai trò:\n{target.role.value}\n\n"
        "Dữ liệu lịch sử của người dùng sẽ được giữ nguyên.\n\n"
        "Sau khi xóa, tài khoản sẽ không còn xuất hiện trong danh sách hoạt động và không thể tiếp tục sử dụng bot.",
        reply_markup=keyboard); await callback.answer()


@router.callback_query(F.data.startswith("softdelete:confirm:"))
async def soft_delete_confirm(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    try: target = await session.get(User, int(callback.data.rsplit(":", 1)[1]))
    except (ValueError, AttributeError): target = None
    if not actor or actor.role != UserRole.SUPER_ADMIN: return await deny(callback)
    if not target: return await callback.answer("Người dùng không tồn tại.", show_alert=True)
    if target.role == UserRole.SUPER_ADMIN: return await callback.answer("❌ Không thể xóa Admin chính.", show_alert=True)
    if target.is_deleted: return await callback.answer("Tài khoản đã được xóa trước đó.", show_alert=True)
    await soft_delete_user(session, actor, target)
    await callback.message.edit_text(f"✅ Đã xóa mềm {target.display_name}.\n\nToàn bộ dữ liệu lịch sử vẫn được giữ nguyên.")
    await callback.answer()


@router.message(F.text == "🗑 Người dùng đã xóa")
async def deleted_users(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: return await deny(message)
    users = list((await session.scalars(select(User).where(User.is_deleted.is_(True)).order_by(User.deleted_at.desc()))).all())
    if not users: return await message.answer("Chưa có người dùng đã xóa.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{u.display_name} · {u.deleted_at:%d/%m/%Y %H:%M}" if u.deleted_at else u.display_name,
                             callback_data=f"deleted:info:{u.id}")
    ] for u in users])
    await message.answer("🗑 NGƯỜI DÙNG ĐÃ XÓA\n\nChọn tài khoản để xem lịch sử:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("deleted:info:"))
async def deleted_user_info(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    try: user = await session.get(User, int(callback.data.rsplit(":", 1)[1]))
    except (ValueError, AttributeError): user = None
    if not user or not user.is_deleted: return await callback.answer("Tài khoản không thuộc danh sách đã xóa.", show_alert=True)
    await callback.message.edit_text(
        (await user_details(user, session)) + "\n\n🔒 Chỉ đọc — tài khoản đã xóa.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📜 Lịch sử theo ngày", callback_data=f"hist:m:{user.id}:d")
        ]]),
    )
    await callback.answer()


def reactivation_decision_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Duyệt", callback_data=f"reactivate:approve:{user_id}"),
         InlineKeyboardButton(text="❌ Từ chối", callback_data=f"reactivate:reject:{user_id}")],
    ])


@router.message(F.text == "📨 Yêu cầu chờ duyệt")
async def pending_reactivations(message: Message, session: AsyncSession, settings: Settings) -> None:
    if message.from_user.id not in settings.super_admin_ids: return await deny(message)
    users = list((await session.scalars(select(User).where(
        User.is_deleted.is_(True), User.reactivation_requested_at.is_not(None)
    ).order_by(User.reactivation_requested_at))).all())
    if not users: return await message.answer("Không có yêu cầu nào đang chờ duyệt.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=user.display_name, callback_data=f"reactivate:view:{user.id}")
    ] for user in users])
    await message.answer("📨 YÊU CẦU CHỜ DUYỆT\n\nChọn người dùng:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("reactivate:view:"))
async def reactivation_view(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    user = await session.get(User, int(callback.data.rsplit(":", 1)[1]))
    if not user or not user.is_deleted or user.reactivation_requested_at is None:
        return await callback.answer("Yêu cầu không còn chờ duyệt.", show_alert=True)
    await callback.message.edit_text(
        f"📨 YÊU CẦU CHỜ DUYỆT\n\nTên: {user.display_name}\nTelegram ID: {user.telegram_id}\n"
        f"Vai trò: {user.role.value}\nNgày yêu cầu: {user.reactivation_requested_at:%d/%m/%Y %H:%M}",
        reply_markup=reactivation_decision_keyboard(user.id),
    ); await callback.answer()


@router.callback_query(F.data.regexp(r"^reactivate:(approve|reject):\d+$"))
async def reactivation_decide(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if callback.from_user.id not in settings.super_admin_ids: return await deny(callback)
    _, decision, raw_id = callback.data.split(":")
    actor = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    target = await session.get(User, int(raw_id))
    if not actor or actor.role != UserRole.SUPER_ADMIN: return await deny(callback)
    if not target: return await callback.answer("Người dùng không tồn tại.", show_alert=True)
    approve = decision == "approve"
    try: await decide_reactivation(session, actor, target, approve=approve)
    except ValueError as exc: return await callback.answer(str(exc), show_alert=True)
    if approve:
        text = ("✅ YÊU CẦU ĐÃ ĐƯỢC DUYỆT\n\nBạn đã được phép sử dụng bot trở lại.\n\n"
                "Toàn bộ dữ liệu và lịch sử trước đây của bạn vẫn được giữ nguyên.")
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Vào menu chính", callback_data="reactivate:menu")
        ]])
        admin_notice = f"✅ Đã duyệt {target.display_name} sử dụng lại bot."
    else:
        text = "❌ Yêu cầu sử dụng lại bot chưa được Admin chấp thuận.\n\nBạn có thể gửi lại yêu cầu sau."
        markup = None
        admin_notice = f"❌ Đã từ chối yêu cầu của {target.display_name}."
    try: await callback.bot.send_message(target.telegram_id, text, reply_markup=markup)
    except Exception: logger.exception("Không gửi được kết quả duyệt lại cho user %s", target.telegram_id)
    await callback.message.edit_text(admin_notice); await callback.answer()
