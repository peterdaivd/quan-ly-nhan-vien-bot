from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from payos import AsyncPayOS, WebhookError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import uvicorn

from app.config import Settings
from app.services.deposit_service import (
    expire_pending_deposits, process_successful_deposit,
)
from app.models import AdminPayment, AdminPaymentStatus, PaymentReminderDelivery, PaymentReminderSchedule, User, UserRole
from app.services.admin_payment_service import daily_settlement, get_payment_receiver, process_successful_admin_payment
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.payos_service import payment_data_dict
from app.utils.money import format_money

logger = logging.getLogger(__name__)


async def notify_paid(bot: Bot, result) -> None:
    if result.outcome != "PAID" or not result.telegram_id or not result.credit:
        return
    try:
        await bot.send_message(
            result.telegram_id,
            "✅ NẠP TIỀN THÀNH CÔNG\n\n"
            f"💵 Số tiền: {format_money(result.amount or 0)}\n\n"
            f"💳 Số dư trước: {format_money(result.credit.balance_before)}\n"
            f"💳 Số dư hiện tại: {format_money(result.credit.balance_after)}\n\n"
            "🏦 Phương thức: BIDV / payOS\n\n"
            f"Mã giao dịch: {result.reference}",
        )
    except Exception:
        # Database đã commit; lỗi Telegram không được làm webhook cộng lại.
        logger.exception("Không gửi được thông báo nạp payOS tới Telegram user")


async def notify_admin_paid(bot: Bot, result, super_admin_ids: frozenset[int]) -> None:
    if result.outcome != "PAID":
        return
    if result.admin_telegram_id:
        try:
            await bot.send_message(result.admin_telegram_id, "✅ NỘP TIỀN THÀNH CÔNG\n\n"
                                   f"Số tiền: {format_money(result.amount or 0)}\n\n"
                                   f"Đã nộp Admin tổng: {format_money(result.amount or 0)}\n"
                                   f"Còn phải nộp: {format_money(result.outstanding or 0)}")
            logger.info("ADMIN NOTIFICATION SENT telegram_id=%s", result.admin_telegram_id)
        except Exception:
            logger.exception("Không gửi được thông báo cho Admin")
    text = ("✅ NHẬN TIỀN TỪ ADMIN\n\n" f"Admin: {result.admin_name or 'Không xác định'}\n\n"
            f"Số tiền: {format_money(result.amount or 0)}\n\nReference: {result.reference}\n"
            f"Còn phải nộp: {format_money(result.outstanding or 0)}")
    for telegram_id in super_admin_ids:
        try:
            await bot.send_message(telegram_id, text)
            logger.info("SUPER_ADMIN NOTIFICATION SENT telegram_id=%s", telegram_id)
        except Exception:
            logger.exception("Không gửi được thông báo cho SUPER_ADMIN %s", telegram_id)


async def process_successful_payment(session: AsyncSession, *, order_code: int, amount: int,
                                     reference: str, payment_link_id: str, raw_data: dict):
    admin_order = await session.scalar(select(AdminPayment.id).where(AdminPayment.order_code == order_code))
    if admin_order:
        logger.info("PAYMENT FOUND order_code=%s", order_code)
        logger.info("PAYMENT TYPE=ADMIN_PAYMENT order_code=%s", order_code)
        result = await process_successful_admin_payment(session, order_code=order_code, amount=amount,
            reference=reference, payment_link_id=payment_link_id, raw_data=raw_data)
        if result.outcome == "PAID":
            logger.info("PAYMENT MARKED PAID order_code=%s reference=%s", order_code, reference)
            logger.info("ADMIN OUTSTANDING UPDATED payer=%s outstanding=%s", result.admin_name, result.outstanding)
        return "ADMIN_PAYMENT", result
    from app.models import Deposit
    deposit_id = await session.scalar(select(Deposit.id).where(Deposit.order_code == order_code))
    if not deposit_id:
        logger.warning("PAYMENT NOT FOUND FOR ORDER_CODE=%s", order_code)
    else:
        logger.info("PAYMENT FOUND order_code=%s", order_code)
        logger.info("PAYMENT TYPE=USER_DEPOSIT order_code=%s", order_code)
    result = await process_successful_deposit(session, order_code=order_code, amount=amount,
        reference=reference, payment_link_id=payment_link_id, raw_data=raw_data)
    return "USER_DEPOSIT", result


def create_backend_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    payos: AsyncPayOS,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def expire_loop() -> None:
            while True:
                try:
                    async with session_factory() as session:
                        await expire_pending_deposits(session)
                except Exception:
                    logger.exception("Không thể cập nhật deposit payOS hết hạn")
                await asyncio.sleep(60)

        async def reconcile_loop() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    async with session_factory() as session:
                        pending = list((await session.scalars(select(AdminPayment).where(
                            AdminPayment.status == AdminPaymentStatus.PENDING,
                            AdminPayment.payment_method == "PAYOS",
                            AdminPayment.payos_payment_link_id.is_not(None),
                        ))).all())
                        for item in pending:
                            payment = await payos.payment_requests.get(item.order_code)
                            if payment.status != "PAID" or not payment.transactions:
                                continue
                            transaction = next((x for x in payment.transactions if x.amount == item.amount), payment.transactions[0])
                            payment_type, result = await process_successful_payment(session,
                                order_code=payment.order_code, amount=transaction.amount,
                                reference=transaction.reference, payment_link_id=payment.id,
                                raw_data=payment_data_dict(payment))
                            if payment_type == "ADMIN_PAYMENT": await notify_admin_paid(bot, result, settings.super_admin_ids)
                            else: await notify_paid(bot, result)
                except Exception:
                    logger.exception("PAYOS RECONCILIATION FAILED")

        async def reminder_loop() -> None:
            timezone = ZoneInfo("Asia/Ho_Chi_Minh")
            while True:
                try:
                    now = datetime.now(timezone); day = now.date()
                    async with session_factory() as session:
                        schedules = list((await session.scalars(select(PaymentReminderSchedule).where(
                            PaymentReminderSchedule.is_active.is_(True), PaymentReminderSchedule.hour == now.hour,
                            PaymentReminderSchedule.minute == now.minute,
                        ))).all())
                        users = list((await session.scalars(select(User).where(User.is_deleted.is_(False), User.role != UserRole.SUPER_ADMIN))).all())
                        for schedule in schedules:
                            for user in users:
                                already = await session.scalar(select(PaymentReminderDelivery.id).where(
                                    PaymentReminderDelivery.schedule_id == schedule.id,
                                    PaymentReminderDelivery.user_id == user.id,
                                    PaymentReminderDelivery.settlement_date == day,
                                ))
                                if already: continue
                                receiver = await get_payment_receiver(session, user)
                                if not receiver: continue
                                summary = await daily_settlement(session, user, receiver, day)
                                if summary.remaining <= 0: continue
                                receiver_name = "Admin tổng" if receiver.role == UserRole.SUPER_ADMIN else receiver.display_name
                                text = (f"⏰ NHẮC NỘP TIỀN\n\n📅 Ngày {day:%d/%m/%Y}\n\n"
                                        f"🚚 Tiền ship: {format_money(summary.shipping)}\n"
                                        f"💵 Phải nộp: {format_money(summary.due)}\n✅ Đã nộp: {format_money(summary.paid)}\n"
                                        f"⏳ Còn lại: {format_money(summary.remaining)}\n\nNgười nhận: {receiver_name}\n\n"
                                        f"Vui lòng hoàn tất nộp tiền cho {receiver_name}.")
                                callback_data = "settlement:manual:create" if user.manager_admin_id else "settlement:payos:create:all"
                                try:
                                    await bot.send_message(user.telegram_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                        InlineKeyboardButton(text="💳 Nộp tiền ngay", callback_data=callback_data)
                                    ]]))
                                    session.add(PaymentReminderDelivery(schedule_id=schedule.id, user_id=user.id, settlement_date=day))
                                    await session.commit()
                                except Exception:
                                    await session.rollback(); logger.exception("Không gửi được reminder user=%s", user.id)
                except Exception:
                    logger.exception("PAYMENT REMINDER LOOP FAILED")
                await asyncio.sleep(20)

        expiry_task = asyncio.create_task(expire_loop(), name="payos-deposit-expiry")
        reconcile_task = asyncio.create_task(reconcile_loop(), name="payos-payment-reconcile")
        reminder_task = asyncio.create_task(reminder_loop(), name="payment-reminder-scheduler")
        yield
        expiry_task.cancel()
        reconcile_task.cancel()
        reminder_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="Telegram Bot payOS Backend", lifespan=lifespan)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "payment_mode": settings.payment_mode}

    @app.post("/api/payos/webhook")
    async def payos_webhook(request: Request):
        raw_body = await request.body()
        try:
            unverified = json.loads(raw_body)
            safe_data = unverified.get("data", {}) if isinstance(unverified, dict) else {}
            logger.info("PAYOS WEBHOOK RECEIVED orderCode=%s amount=%s reference=%s paymentLinkId=%s code=%s",
                        safe_data.get("orderCode"), safe_data.get("amount"), safe_data.get("reference"),
                        safe_data.get("paymentLinkId"), unverified.get("code") if isinstance(unverified, dict) else None)
        except Exception:
            logger.info("PAYOS WEBHOOK RECEIVED payload_unreadable=true")
        try:
            verified = await payos.webhooks.verify(raw_body)
        except WebhookError as exc:
            logger.exception("PAYOS WEBHOOK VERIFY FAILED: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid payOS webhook") from exc
        except Exception as exc:
            logger.exception("PAYOS WEBHOOK VERIFY FAILED: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid payOS webhook") from exc

        logger.info("PAYOS WEBHOOK VERIFIED orderCode=%s amount=%s reference=%s paymentLinkId=%s code=%s",
                    verified.order_code, verified.amount, verified.reference, verified.payment_link_id, verified.code)
        if verified.code != "00" or verified.currency != "VND":
            return {"success": True, "outcome": "IGNORED"}
        raw_verified = payment_data_dict(verified)
        async with session_factory() as session:
            payment_type, result = await process_successful_payment(session, order_code=verified.order_code,
                amount=verified.amount, reference=verified.reference,
                payment_link_id=verified.payment_link_id, raw_data=raw_verified)
        if payment_type == "ADMIN_PAYMENT":
            await notify_admin_paid(bot, result, settings.super_admin_ids)
        else:
            await notify_paid(bot, result)
        # Webhook hợp lệ luôn trả 200, kể cả duplicate/not-found để payOS không retry vô hạn.
        return {"success": True, "outcome": result.outcome}

    @app.get("/payment/success", response_class=HTMLResponse)
    async def payment_success():
        return "<h2>Thanh toán đã được ghi nhận</h2><p>Bạn có thể quay lại Telegram. Ví chỉ được cộng sau khi backend xác minh với payOS.</p>"

    @app.get("/payment/cancel", response_class=HTMLResponse)
    async def payment_cancel():
        return "<h2>Thanh toán đã bị hủy</h2><p>Bạn có thể quay lại Telegram để tạo yêu cầu mới.</p>"

    return app


async def start_backend(app: FastAPI, settings: Settings):
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="fastapi-uvicorn")
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if task.done():
        await task
    logger.info("payOS webhook URL: %s", settings.payos_webhook_url)
    return server, task


async def stop_backend(server: uvicorn.Server, task: asyncio.Task) -> None:
    server.should_exit = True
    await task
