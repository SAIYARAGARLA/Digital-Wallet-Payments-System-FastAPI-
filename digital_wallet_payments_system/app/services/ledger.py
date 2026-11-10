from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models import Account, LedgerEntry, Wallet, WalletTransaction

SYSTEM_TYPES = ["Bank Settlement", "Cashback Pool", "Fees", "Withdrawal Clearing", "Hold"]

def account_for_wallet(db: Session, wallet: Wallet) -> Account:
    acct = db.scalar(select(Account).where(Account.wallet_id == wallet.id))
    if not acct:
        acct = Account(account_type="User Wallet", wallet_id=wallet.id, name=wallet.wallet_number)
        db.add(acct); db.flush()
    return acct

def system_account(db: Session, kind: str) -> Account:
    acct = db.scalar(select(Account).where(Account.account_type == kind, Account.wallet_id.is_(None)))
    if not acct:
        acct = Account(account_type=kind, wallet_id=None, name=kind)
        db.add(acct); db.flush()
    return acct

def post_pair(db: Session, tx: WalletTransaction, debit: Account, credit: Account, amount: Decimal, debit_balance: Decimal, credit_balance: Decimal):
    amount = Decimal(amount).quantize(Decimal("0.01"))
    db.add_all([
        LedgerEntry(transaction=tx, account_id=debit.id, entry_type="Debit", amount=amount, balance_after=debit_balance),
        LedgerEntry(transaction=tx, account_id=credit.id, entry_type="Credit", amount=amount, balance_after=credit_balance)
    ])

def move_between_wallets(db: Session, tx: WalletTransaction, sender: Wallet, receiver: Wallet, amount: Decimal):
    # Lock in ascending primary-key order to reduce deadlock risk on MySQL/InnoDB.
    ids = sorted([sender.id, receiver.id])
    locked = db.scalars(select(Wallet).where(Wallet.id.in_(ids)).order_by(Wallet.id).with_for_update()).all()
    by_id = {w.id: w for w in locked}
    sender, receiver = by_id[sender.id], by_id[receiver.id]
    if sender.status != "Active": raise HTTPException(423, "Sender wallet is frozen")
    if sender.balance < amount: raise HTTPException(400, "Insufficient balance")
    limit = Decimal("10000") if receiver.kyc_level == "Minimum" else Decimal("200000")
    if receiver.balance + amount > limit: raise HTTPException(400, "Receiver wallet KYC balance limit exceeded")
    sender.balance -= amount; receiver.balance += amount
    post_pair(db, tx, account_for_wallet(db, sender), account_for_wallet(db, receiver), amount, sender.balance, receiver.balance)
    return sender, receiver
