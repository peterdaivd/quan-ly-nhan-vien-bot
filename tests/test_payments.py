import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile

from httpx import ASGITransport, AsyncClient
from payos import AsyncPayOS
from payos.types.webhooks.webhook import WebhookData
from sqlalchemy import func, select

from app.backend import create_backend_app
from app.config import Settings
from app.database import create_database, initialize_database
from app.models import (
    Deposit, DepositStatus, PaymentEvent, User, Wallet, WalletTransaction,
)
from app.services import deposit_service
from app.services.deposit_service import process_successful_deposit


def settings(database_url: str) -> Settings:
    return Settings(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        admin_ids=frozenset({999}), database_url=database_url,
        payment_mode="PAYOS", payos_client_id="client", payos_api_key="api-key",
        payos_checksum_key="checksum", payos_webhook_url="https://example.com/api/payos/webhook",
        payos_return_url="https://example.com/payment/success",
        payos_cancel_url="https://example.com/payment/cancel",
        payos_payment_expires_minutes=30, api_host="127.0.0.1", api_port=8080,
    )


def add_deposit(
    session, user_id: int, order_code: int, amount: int = 500_000,
    status: DepositStatus = DepositStatus.PENDING, link_id: str = "link-1",
) -> Deposit:
    item = Deposit(
        user_id=user_id, order_code=order_code, amount=amount, status=status,
        payos_payment_link_id=link_id, checkout_url="https://pay.payos.vn/x",
        qr_code="qr", expires_at=datetime.now() + timedelta(minutes=30),
    )
    session.add(item)
    return item


def test_success_duplicate_and_two_user_isolation() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_url = f"sqlite+aiosqlite:///{(Path(temp_dir) / 'payos.db').as_posix()}"
            engine, factory = create_database(db_url)
            await initialize_database(engine)
            async with factory() as session:
                user_a = User(telegram_id=100, first_name="A")
                user_b = User(telegram_id=200, first_name="B")
                session.add_all([user_a, user_b])
                await session.flush()
                session.add(Wallet(user_id=user_a.id, balance=1_000_000, total_deposited=1_000_000))
                deposit_a = add_deposit(session, user_a.id, 1001)
                add_deposit(session, user_b.id, 1002)
                await session.commit()
                user_a_id, user_b_id, deposit_a_id = user_a.id, user_b.id, deposit_a.id

                result = await process_successful_deposit(
                    session, order_code=1001, amount=500_000, reference="ABC123",
                    payment_link_id="link-1", raw_data={"source": "test"},
                )
                assert result.outcome == "PAID"
                assert (result.credit.balance_before, result.credit.balance_after) == (1_000_000, 1_500_000)
                assert (await session.get(Deposit, deposit_a_id)).status == DepositStatus.PAID
                assert (await session.scalar(select(Wallet).where(Wallet.user_id == user_a_id))).balance == 1_500_000
                assert await session.scalar(select(Wallet).where(Wallet.user_id == user_b_id)) is None
                assert await session.scalar(select(func.count(WalletTransaction.id))) == 1

                duplicate = await process_successful_deposit(
                    session, order_code=1001, amount=500_000, reference="ABC123",
                    payment_link_id="link-1", raw_data={"source": "retry"},
                )
                assert duplicate.outcome == "DUPLICATE"
                assert (await session.scalar(select(Wallet).where(Wallet.user_id == user_a_id))).balance == 1_500_000
                assert await session.scalar(select(func.count(WalletTransaction.id))) == 1

                paid_b = await process_successful_deposit(
                    session, order_code=1002, amount=500_000, reference="B-REF",
                    payment_link_id="link-1", raw_data={},
                )
                assert paid_b.telegram_id == 200
                assert (await session.scalar(select(Wallet).where(Wallet.user_id == user_b_id))).balance == 500_000
            await engine.dispose()
    asyncio.run(scenario())


def test_rejection_cases() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_url = f"sqlite+aiosqlite:///{(Path(temp_dir) / 'reject.db').as_posix()}"
            engine, factory = create_database(db_url)
            await initialize_database(engine)
            async with factory() as session:
                user = User(telegram_id=300, first_name="Test")
                session.add(user)
                await session.flush()
                wrong_amount = add_deposit(session, user.id, 2001)
                wrong_link = add_deposit(session, user.id, 2002)
                cancelled = add_deposit(session, user.id, 2003, status=DepositStatus.CANCELLED)
                already_paid = add_deposit(session, user.id, 2004, status=DepositStatus.PAID)
                await session.commit()

                amount_result = await process_successful_deposit(
                    session, order_code=2001, amount=400_000, reference="AMOUNT-WRONG",
                    payment_link_id="link-1", raw_data={},
                )
                assert amount_result.outcome == "REVIEW"
                assert (await session.get(Deposit, wrong_amount.id)).status == DepositStatus.REVIEW

                link_result = await process_successful_deposit(
                    session, order_code=2002, amount=500_000, reference="LINK-WRONG",
                    payment_link_id="another-link", raw_data={},
                )
                assert link_result.outcome == "REVIEW"
                assert (await session.get(Deposit, wrong_link.id)).status == DepositStatus.REVIEW

                missing = await process_successful_deposit(
                    session, order_code=999999, amount=500_000, reference="ORDER-WRONG",
                    payment_link_id="link-1", raw_data={},
                )
                assert missing.outcome == "REJECTED"
                for order_code, reference in [(2003, "CANCELLED-REF"), (2004, "PAID-REF")]:
                    result = await process_successful_deposit(
                        session, order_code=order_code, amount=500_000,
                        reference=reference, payment_link_id="link-1", raw_data={},
                    )
                    assert result.outcome in {"REJECTED", "DUPLICATE"}
                assert await session.scalar(select(func.count(WalletTransaction.id))) == 0
                assert await session.scalar(select(func.count(PaymentEvent.id))) == 5
            await engine.dispose()
    asyncio.run(scenario())


def test_database_failure_rolls_back(monkeypatch) -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_url = f"sqlite+aiosqlite:///{(Path(temp_dir) / 'rollback.db').as_posix()}"
            engine, factory = create_database(db_url)
            await initialize_database(engine)
            async with factory() as session:
                user = User(telegram_id=400, first_name="Rollback")
                session.add(user)
                await session.flush()
                wallet = Wallet(user_id=user.id, balance=1_000_000, total_deposited=1_000_000)
                session.add(wallet)
                deposit = add_deposit(session, user.id, 3001)
                await session.commit()
                user_id, deposit_id = user.id, deposit.id

                async def broken_credit(active_session, active_deposit, reference):
                    current = await active_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
                    current.balance += active_deposit.amount
                    raise RuntimeError("simulated DB failure")

                monkeypatch.setattr(deposit_service, "credit_paid_deposit", broken_credit)
                try:
                    await process_successful_deposit(
                        session, order_code=3001, amount=500_000, reference="ROLLBACK-REF",
                        payment_link_id="link-1", raw_data={},
                    )
                    raise AssertionError("Lỗi giả lập phải được phát ra")
                except RuntimeError:
                    await session.rollback()
                assert (await session.scalar(select(Wallet).where(Wallet.user_id == user_id))).balance == 1_000_000
                assert (await session.get(Deposit, deposit_id)).status == DepositStatus.PENDING
                assert await session.scalar(select(func.count(PaymentEvent.id))) == 0
            await engine.dispose()
    asyncio.run(scenario())


def test_invalid_webhook_returns_400_and_does_not_credit() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_url = f"sqlite+aiosqlite:///{(Path(temp_dir) / 'webhook.db').as_posix()}"
            config = settings(db_url)
            engine, factory = create_database(db_url)
            await initialize_database(engine)

            class FakeWebhooks:
                async def verify(self, _payload):
                    raise ValueError("invalid signature")

            class FakePayOS:
                webhooks = FakeWebhooks()

            class FakeBot:
                async def send_message(self, *_args, **_kwargs):
                    raise AssertionError("Không được gửi Telegram")

            app = create_backend_app(config, factory, FakeBot(), FakePayOS())
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/payos/webhook", content=b'{"signature":"bad"}')
            assert response.status_code == 400
            async with factory() as session:
                assert await session.scalar(select(func.count(WalletTransaction.id))) == 0
            await engine.dispose()
    asyncio.run(scenario())


def test_real_sdk_verified_webhook_credits_wallet() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_url = f"sqlite+aiosqlite:///{(Path(temp_dir) / 'verified.db').as_posix()}"
            config = settings(db_url)
            engine, factory = create_database(db_url)
            await initialize_database(engine)
            async with factory() as session:
                user = User(telegram_id=500, first_name="Verified")
                session.add(user)
                await session.flush()
                session.add(Wallet(user_id=user.id, balance=1_000_000, total_deposited=1_000_000))
                add_deposit(session, user.id, 4001, link_id="payos-link-4001")
                await session.commit()

            payos = AsyncPayOS("client", "api-key", "checksum")
            data = {
                "orderCode": 4001, "amount": 500_000, "description": "NAP1",
                "accountNumber": "0000000000", "reference": "ABC-VERIFIED",
                "transactionDateTime": "2026-09-16 10:00:00", "currency": "VND",
                "paymentLinkId": "payos-link-4001", "code": "00", "desc": "success",
            }
            normalized_data = WebhookData(**data).model_dump_camel_case()
            signature = payos.crypto.create_signature_from_object(normalized_data, "checksum")
            webhook = {
                "code": "00", "desc": "success", "success": True,
                "data": data, "signature": signature,
            }

            class FakeBot:
                sent = []

                async def send_message(self, telegram_id, text):
                    self.sent.append((telegram_id, text))

            bot = FakeBot()
            app = create_backend_app(config, factory, bot, payos)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/payos/webhook", json=webhook)
            assert response.status_code == 200
            assert response.json()["outcome"] == "PAID"
            async with factory() as session:
                assert (await session.scalar(select(Wallet).where(Wallet.user_id == 1))).balance == 1_500_000
                assert await session.scalar(select(func.count(WalletTransaction.id))) == 1
            assert bot.sent and bot.sent[0][0] == 500
            await payos.aclose()
            await engine.dispose()
    asyncio.run(scenario())
