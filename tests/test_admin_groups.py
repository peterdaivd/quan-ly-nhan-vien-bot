import asyncio
from pathlib import Path
import tempfile
from datetime import date

from sqlalchemy import func, select

from app.database import create_database, initialize_database
from app.models import AdminPayment, AdminPaymentStatus, CommissionType, DailySale, Product, User, UserRole, Wallet, WalletTransaction
from app.services.admin_payment_service import (
    amount_due_to_receiver, confirm_manual_settlement, create_admin_payment, create_settlement, daily_settlement,
    get_payment_receiver, paid_admin_total, personal_company_total, process_successful_admin_payment,
    received_from_staff_total,
)
from app.utils.permissions import can_edit_commission, can_manage_employee


def test_admin_scope_group_total_and_payment_does_not_credit_wallet() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as folder:
            url = f"sqlite+aiosqlite:///{(Path(folder) / 'groups.db').as_posix()}"
            engine, factory = create_database(url)
            await initialize_database(engine)
            async with factory() as session:
                super_admin = User(telegram_id=1, first_name="Owner", role=UserRole.SUPER_ADMIN)
                admin_a = User(telegram_id=2, first_name="Admin A", role=UserRole.ADMIN)
                admin_b = User(telegram_id=3, first_name="Admin B", role=UserRole.ADMIN)
                session.add_all([super_admin, admin_a, admin_b]); await session.flush()
                user_a = User(telegram_id=4, first_name="A", manager_admin_id=admin_a.id)
                user_b = User(telegram_id=5, first_name="B", manager_admin_id=admin_b.id)
                session.add_all([user_a, user_b]); await session.flush()
                product = Product(name="Test", price=100_000, commission_type=CommissionType.PERCENT, commission_value=1000)
                session.add(product); await session.flush()
                session.add_all([
                    DailySale(user_id=user_a.id, product_id=product.id, quantity_sold=2, revenue=200_000,
                              commission_amount=20_000, company_amount=180_000),
                    DailySale(user_id=user_b.id, product_id=product.id, quantity_sold=9, revenue=900_000,
                              commission_amount=90_000, company_amount=810_000),
                    DailySale(user_id=admin_a.id, product_id=product.id, quantity_sold=1, revenue=60_000,
                              commission_amount=10_000, company_amount=50_000),
                    DailySale(user_id=admin_b.id, product_id=product.id, quantity_sold=1, revenue=550_000,
                              commission_amount=77_000, company_amount=473_000),
                    Wallet(user_id=admin_a.id, balance=123_000, total_deposited=123_000),
                ])
                await session.commit()
                assert can_manage_employee(admin_a, user_a)
                assert not can_manage_employee(admin_a, user_b)
                assert can_manage_employee(super_admin, user_b)
                assert can_edit_commission(super_admin)
                assert not can_edit_commission(admin_a)
                assert (await get_payment_receiver(session, user_a)).id == admin_a.id
                assert (await get_payment_receiver(session, user_b)).id == admin_b.id
                assert (await get_payment_receiver(session, admin_a)).id == super_admin.id
                assert await personal_company_total(session, user_a.id) == 180_000

                staff_payment = await create_settlement(session, user_a.id, admin_a.id, 180_000, payment_method="MANUAL")
                try:
                    await confirm_manual_settlement(session, admin_b, staff_payment.id)
                    raise AssertionError("Admin khác không được xác nhận")
                except PermissionError:
                    pass
                await confirm_manual_settlement(session, admin_a, staff_payment.id)
                assert await received_from_staff_total(session, admin_a.id) == 180_000
                assert await amount_due_to_receiver(session, admin_a, super_admin) == 230_000

                payment = await create_admin_payment(session, admin_a.id, 230_000, super_admin.id)
                payment.payos_payment_link_id = "admin-link"
                await session.commit()
                result = await process_successful_admin_payment(
                    session, order_code=payment.order_code, amount=230_000,
                    reference="ADMIN-REF", payment_link_id="admin-link", raw_data={"ok": True},
                )
                assert result.outcome == "PAID"
                assert (await session.get(AdminPayment, payment.id)).status == AdminPaymentStatus.PAID
                assert await paid_admin_total(session, admin_a.id) == 230_000
                assert await amount_due_to_receiver(session, admin_a, super_admin) == 0
                assert (await session.scalar(select(Wallet).where(Wallet.user_id == admin_a.id))).balance == 123_000
                assert await session.scalar(select(func.count(WalletTransaction.id))) == 0
                duplicate = await process_successful_admin_payment(
                    session, order_code=payment.order_code, amount=230_000,
                    reference="ADMIN-REF", payment_link_id="admin-link", raw_data={},
                )
                assert duplicate.outcome == "DUPLICATE"

                assert await amount_due_to_receiver(session, admin_b, super_admin) == 473_000
                partial = await create_admin_payment(session, admin_b.id, 10_000, super_admin.id)
                partial.payos_payment_link_id = "partial-link"; await session.commit()
                paid_partial = await process_successful_admin_payment(
                    session, order_code=partial.order_code, amount=10_000, reference="PARTIAL-REF",
                    payment_link_id="partial-link", raw_data={},
                )
                assert paid_partial.outcome == "PAID" and paid_partial.outstanding == 463_000
                assert await amount_due_to_receiver(session, admin_b, super_admin) == 463_000
                duplicate_partial = await process_successful_admin_payment(
                    session, order_code=partial.order_code, amount=10_000, reference="PARTIAL-REF",
                    payment_link_id="partial-link", raw_data={},
                )
                assert duplicate_partial.outcome == "DUPLICATE"
                assert await paid_admin_total(session, admin_b.id) == 10_000
                sale_snapshot = await session.scalar(select(DailySale).where(DailySale.user_id == admin_b.id))
                assert (sale_snapshot.revenue, sale_snapshot.commission_amount, sale_snapshot.company_amount) == (550_000, 77_000, 473_000)
                daily = await daily_settlement(session, admin_b, super_admin, date.today())
                assert (daily.due, daily.paid, daily.remaining) == (473_000, 10_000, 463_000)
                for amount, reference, expected_paid, expected_remaining in [
                    (200_000, "PARTIAL-2", 210_000, 263_000),
                    (263_000, "PARTIAL-3", 473_000, 0),
                ]:
                    part = await create_admin_payment(session, admin_b.id, amount, super_admin.id)
                    part.payos_payment_link_id = reference; await session.commit()
                    result = await process_successful_admin_payment(session, order_code=part.order_code, amount=amount,
                        reference=reference, payment_link_id=reference, raw_data={})
                    assert result.outcome == "PAID"
                    daily = await daily_settlement(session, admin_b, super_admin, date.today())
                    assert (daily.paid, daily.remaining) == (expected_paid, expected_remaining)
            await engine.dispose()
    asyncio.run(scenario())
