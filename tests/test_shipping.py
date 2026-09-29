import asyncio
from datetime import date
from pathlib import Path
import tempfile

from sqlalchemy import func, select

from app.database import create_database, initialize_database
from app.models import DailySale, DailyShippingFee, User, UserRole
from app.services.admin_payment_service import (
    create_settlement, daily_settlement, get_payment_receiver, settlement_totals,
)
from app.services.shipping_service import get_shipping_fee, get_shipping_total, set_shipping_fee


def test_daily_shipping_upsert_ranges_due_and_receiver() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as folder:
            engine, factory = create_database(f"sqlite+aiosqlite:///{(Path(folder) / 'shipping.db').as_posix()}")
            await initialize_database(engine)
            async with factory() as session:
                owner = User(telegram_id=1, first_name="Owner", role=UserRole.SUPER_ADMIN)
                admin = User(telegram_id=2, first_name="Admin C", role=UserRole.ADMIN)
                c1 = User(telegram_id=3, first_name="C1", role=UserRole.USER)
                session.add_all([owner, admin, c1]); await session.flush()
                c1.manager_admin_id = admin.id

                # product_id chỉ phục vụ FK của daily_sales trong schema test.
                from app.models import CommissionType, Product
                product = Product(name="Test", price=1_000_000,
                                  commission_type=CommissionType.FIXED_PER_ITEM, commission_value=0)
                session.add(product); await session.flush()
                day_29, day_30, day_01 = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)
                for day, revenue, commission in [
                    (day_29, 1_000_000, 100_000),
                    (day_30, 500_000, 50_000),
                    (day_01, 200_000, 20_000),
                ]:
                    session.add(DailySale(
                        user_id=c1.id, product_id=product.id, sale_date=day, quantity_sold=1,
                        revenue=revenue, commission_amount=commission,
                        company_amount=revenue - commission,
                    ))
                await session.commit()

                await set_shipping_fee(session, c1.id, day_29, 30_000)
                await set_shipping_fee(session, c1.id, day_01, 20_000)

                case1 = await settlement_totals(session, c1.id, day_29, day_29)
                assert (case1.revenue, case1.commission, case1.shipping, case1.due) == (
                    1_000_000, 100_000, 30_000, 870_000,
                )
                case2 = await settlement_totals(session, c1.id, day_30, day_30)
                assert (case2.shipping, case2.due) == (0, 450_000)
                assert await get_shipping_fee(session, c1.id, day_30) == 0
                assert await get_shipping_total(session, c1.id, day_29, day_01) == 50_000

                # Upsert thay thế, không cộng dồn và không tạo dòng trùng.
                await set_shipping_fee(session, c1.id, day_29, 40_000)
                assert await get_shipping_fee(session, c1.id, day_29) == 40_000
                assert await session.scalar(select(func.count(DailyShippingFee.id)).where(
                    DailyShippingFee.user_id == c1.id,
                    DailyShippingFee.business_date == day_29,
                )) == 1

                receiver = await get_payment_receiver(session, c1)
                assert receiver.id == admin.id
                summary = await daily_settlement(session, c1, receiver, day_29)
                assert (summary.shipping, summary.due, summary.remaining) == (40_000, 860_000, 860_000)

                # Payment đã tạo giữ nguyên amount snapshot dù ship được sửa sau đó.
                payment = await create_settlement(
                    session, c1.id, receiver.id, summary.remaining,
                    payment_method="MANUAL", settlement_date=day_29,
                )
                await set_shipping_fee(session, c1.id, day_29, 50_000)
                assert payment.amount == 860_000
                assert (await get_payment_receiver(session, c1)).id == admin.id
            await engine.dispose()
    asyncio.run(scenario())
