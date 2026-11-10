import logging
from datetime import datetime
from decimal import Decimal
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Wallet, LedgerEntry, Account, MoneyRequest

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def expire_requests():
    db = SessionLocal()
    try:
        db.query(MoneyRequest).filter(
            MoneyRequest.status == "Pending",
            MoneyRequest.expires_at < datetime.utcnow(),
        ).update({"status": "Expired"})
        db.commit()
    finally:
        db.close()


def reconcile():
    """Compare wallet balances with ledger net balances and return a summary."""
    db = SessionLocal()
    checked = 0
    mismatches = []
    try:
        wallets = db.scalars(select(Wallet).order_by(Wallet.id)).all()
        for wallet in wallets:
            account = db.scalar(select(Account).where(Account.wallet_id == wallet.id))
            if account is None:
                # A wallet with no ledger account has no posted ledger activity yet.
                if wallet.balance != Decimal("0.00"):
                    mismatches.append({
                        "wallet_id": wallet.id,
                        "wallet_balance": str(wallet.balance),
                        "ledger_balance": "0.00",
                        "reason": "Missing ledger account",
                    })
                checked += 1
                continue

            entries = db.scalars(
                select(LedgerEntry).where(LedgerEntry.account_id == account.id)
            ).all()
            credits = sum(
                (entry.amount for entry in entries if entry.entry_type == "Credit"),
                Decimal("0.00"),
            )
            debits = sum(
                (entry.amount for entry in entries if entry.entry_type == "Debit"),
                Decimal("0.00"),
            )
            ledger_balance = credits - debits
            checked += 1
            if ledger_balance != wallet.balance:
                mismatches.append({
                    "wallet_id": wallet.id,
                    "wallet_balance": str(wallet.balance),
                    "ledger_balance": str(ledger_balance),
                    "reason": "Balance mismatch",
                })

        summary = {
            "status": "PASS" if not mismatches else "MISMATCH",
            "wallets_checked": checked,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
        }
        log.info("Wallet reconciliation result: %s", summary)
        print(f"Wallet reconciliation result: {summary}")
        return summary
    except Exception:
        log.exception("Wallet reconciliation failed")
        raise
    finally:
        db.close()


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(expire_requests, "interval", hours=1, id="expire_requests", replace_existing=True)
    scheduler.add_job(reconcile, "interval", hours=24, id="reconcile", replace_existing=True)
    scheduler.start()
