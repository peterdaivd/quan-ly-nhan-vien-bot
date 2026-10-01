import asyncio
from datetime import timedelta
from pathlib import Path
import tempfile

from sqlalchemy import func, select

from app.database import create_database, initialize_database
from app.models import (
    AdminPayment, AdminPaymentStatus, CommissionType, DailySale, PaymentAdjustment,
    Product, User, UserRole,
)
from app.services.admin_payment_service import debt_summary_to_receiver, get_payment_receiver
from app.services.debt_adjustment_service import can_reset_debt, reset_debt
from app.services.shipping_service import set_shipping_fee, vietnam_today


def test_debt_reset_is_additive_scoped_and_future_debt_still_accrues() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as folder:
            engine, factory = create_database(
                f"sqlite+aiosqlite:///{(Path(folder) / 'debt-reset.db').as_posix()}"
            )
            await initialize_database(engine)
            async with factory() as session:
                owner = User(telegram_id=1, first_name="Owner", role=UserRole.SUPER_ADMIN)
                admin_c = User(telegram_id=2, first_name="Admin C", role=UserRole.ADMIN)
                admin_a = User(telegram_id=3, first_name="Admin A", role=UserRole.ADMIN)
                session.add_all([owner, admin_c, admin_a])
                await session.flush()
                user_c = User(
                    telegram_id=4, first_name="C1", role=UserRole.USER,
                    manager_admin_id=admin_c.id,
                )
                user_a = User(
                    telegram_id=5, first_name="A1", role=UserRole.USER,
                    manager_admin_id=None,
                )
                session.add_all([user_c, user_a])
                product = Product(
                    name="Test", price=1_000_000,
                    commission_type=CommissionType.FIXED_PER_ITEM, commission_value=0,
                )
                session.add(product)
                await session.flush()
                reset_day = vietnam_today()
                session.add(DailySale(
                    user_id=user_c.id, product_id=product.id, sale_date=reset_day,
                    quantity_sold=1, revenue=1_000_000, commission_amount=100_000,
                    company_amount=900_000,
                ))
                await session.commit()
                await set_shipping_fee(session, user_c.id, reset_day, 30_000)

                paid = AdminPayment(
                    admin_user_id=user_c.id, payer_user_id=user_c.id,
                    receiver_user_id=admin_c.id, payment_method="MANUAL",
                    settlement_date=reset_day, amount=500_000, order_code=900_000_000_001,
                    status=AdminPaymentStatus.PAID,
                )
                session.add(paid)
                await session.commit()

                receiver = await get_payment_receiver(session, user_c)
                before = await debt_summary_to_receiver(session, user_c, receiver)
                assert (before.gross_due, before.paid, before.adjusted, before.outstanding) == (
                    870_000, 500_000, 0, 370_000,
                )

                assert can_reset_debt(admin_c, user_c)
                assert not can_reset_debt(admin_c, user_a)
                assert not can_reset_debt(admin_c, admin_a)
                assert can_reset_debt(owner, user_c)
                assert can_reset_debt(owner, admin_c)
                assert not can_reset_debt(owner, owner)

                first = await reset_debt(session, admin_c, user_c)
                assert first.created and first.amount == 370_000
                after = await debt_summary_to_receiver(session, user_c, receiver)
                assert (after.adjusted, after.outstanding) == (370_000, 0)

                count_before = await session.scalar(select(func.count(PaymentAdjustment.id)))
                duplicate = await reset_debt(session, admin_c, user_c)
                count_after = await session.scalar(select(func.count(PaymentAdjustment.id)))
                assert not duplicate.created and duplicate.amount == 0
                assert count_after == count_before == 1

                future_day = reset_day + timedelta(days=1)
                session.add(DailySale(
                    user_id=user_c.id, product_id=product.id, sale_date=future_day,
                    quantity_sold=1, revenue=200_000, commission_amount=20_000,
                    company_amount=180_000,
                ))
                await session.commit()
                await set_shipping_fee(session, user_c.id, future_day, 10_000)
                future = await debt_summary_to_receiver(session, user_c, receiver)
                assert future.outstanding == 170_000

                # SUPER_ADMIN có thể reset USER bất kỳ và ADMIN; ADMIN C không thể reset nhóm khác.
                try:
                    await reset_debt(session, admin_c, user_a)
                    raise AssertionError("ADMIN không được reset USER nộp trực tiếp SUPER_ADMIN")
                except PermissionError:
                    pass
                session.add_all([
                    DailySale(
                        user_id=user_a.id, product_id=product.id, sale_date=reset_day,
                        quantity_sold=1, revenue=100_000, commission_amount=10_000,
                        company_amount=90_000,
                    ),
                    DailySale(
                        user_id=admin_c.id, product_id=product.id, sale_date=reset_day,
                        quantity_sold=1, revenue=50_000, commission_amount=5_000,
                        company_amount=45_000,
                    ),
                ])
                await session.commit()
                assert (await reset_debt(session, owner, user_a)).created
                assert (await reset_debt(session, owner, admin_c)).created
            await engine.dispose()

    asyncio.run(scenario())
