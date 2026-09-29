from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deposit, Wallet, WalletTransaction, WalletTransactionType


@dataclass(frozen=True)
class WalletCreditResult:
    balance_before: int
    balance_after: int


async def get_or_create_wallet(session: AsyncSession, user_id: int) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if wallet is None:
        wallet = Wallet(user_id=user_id, balance=0, total_deposited=0)
        session.add(wallet)
        await session.flush()
    return wallet


async def credit_paid_deposit(
    session: AsyncSession, deposit: Deposit, reference: str
) -> WalletCreditResult:
    """Cộng ví và tạo lịch sử trong transaction do caller quản lý."""
    wallet = await get_or_create_wallet(session, deposit.user_id)
    before = wallet.balance
    after = before + deposit.amount
    wallet.balance = after
    wallet.total_deposited += deposit.amount
    session.add(WalletTransaction(
        user_id=deposit.user_id,
        transaction_type=WalletTransactionType.DEPOSIT,
        amount=deposit.amount,
        balance_before=before,
        balance_after=after,
        provider="PAYOS",
        reference=reference,
        deposit_id=deposit.id,
        description=f"Nạp tiền qua payOS - order {deposit.order_code}",
    ))
    return WalletCreditResult(before, after)


async def wallet_for_user(session: AsyncSession, user_id: int) -> Wallet | None:
    return await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
