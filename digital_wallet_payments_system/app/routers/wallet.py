from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
import hashlib, json, re
from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.database import get_db
from app.models import User, Wallet, WalletTransaction, IdempotencyRecord, KYC, Account, LedgerEntry, OTP
from app.security import current_user, hash_value, verify_value, require_roles
from app.schemas import AddMoneyIn, TransferIn, PinIn, KYCIn
from app.services.ledger import system_account, account_for_wallet, post_pair, move_between_wallets
from app.utils.notifications import send_email

router = APIRouter(tags=["Wallet & Transfers"])

def wallet_of(db, user):
    w = db.scalar(select(Wallet).where(Wallet.owner_id == user.id))
    if not w: raise HTTPException(404, "Wallet not found")
    return w

def check_idempotency(db, user, key, endpoint, body):
    if not key: raise HTTPException(400, "Idempotency-Key header is required")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    old = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == key))
    if old:
        if old.user_id != user.id or old.endpoint != endpoint or old.request_hash != digest: raise HTTPException(422, "Idempotency key reused with a different request")
        if old.state == "Processing": raise HTTPException(409, "Request is still processing")
        from fastapi.responses import JSONResponse
        return JSONResponse(json.loads(old.response_json), status_code=old.status_code or 200, headers={"Idempotent-Replayed":"true"})
    rec = IdempotencyRecord(key=key, user_id=user.id, endpoint=endpoint, request_hash=digest, state="Processing")
    db.add(rec); db.commit()
    return rec

def finish_idempotency(db, rec, response, status=200):
    rec.response_json=json.dumps(response, default=str); rec.status_code=status; rec.state="Completed"; db.commit()

@router.get("/wallet")
def get_wallet(db: Session=Depends(get_db), user=Depends(current_user)):
    w=wallet_of(db,user)
    return {"wallet_number":w.wallet_number,"balance":str(w.balance),"kyc_level":w.kyc_level,"status":w.status}

@router.post("/wallet/pin")
def set_pin(data: PinIn, db: Session=Depends(get_db), user=Depends(current_user)):
    if data.pin in {"1234","1111","0000","2222","3333","4444","5555","6666","7777","8888","9999"}: raise HTTPException(422,"Simple PINs are not allowed")
    user.pin_hash=hash_value(data.pin); db.commit(); return {"message":"Transaction PIN set"}

@router.put("/wallet/pin/change")
def change_pin(data: PinIn, db: Session=Depends(get_db), user=Depends(current_user)):
    user.pin_hash=hash_value(data.pin); db.commit(); return {"message":"PIN changed"}

@router.post("/wallet/add-money")
def add_money(data: AddMoneyIn, background_tasks: BackgroundTasks, idempotency_key: str=Header(None, alias="Idempotency-Key"), db: Session=Depends(get_db), user=Depends(current_user)):
    rec=check_idempotency(db,user,idempotency_key,"/wallet/add-money",data.model_dump())
    if not isinstance(rec,IdempotencyRecord): return rec
    w=wallet_of(db,user); amount=data.amount
    if amount < 10 or amount > 50000: raise HTTPException(400,"Add-money amount must be between ₹10 and ₹50,000")
    limit=Decimal("10000") if w.kyc_level=="Minimum" else Decimal("200000")
    if w.balance+amount>limit: raise HTTPException(400,"KYC wallet balance limit exceeded")
    tx=WalletTransaction(transaction_ref="TXN-"+datetime.utcnow().strftime("%Y%m%d")+"-"+uuid4().hex[:6].upper(),type="Add Money",amount=amount,status="Failed" if data.simulate_status=="Failed" else "Success",initiated_by=user.id,description=f"Add money via {data.source}")
    db.add(tx); db.flush()
    if tx.status=="Success":
        w.balance += amount
        post_pair(db,tx,system_account(db,"Bank Settlement"),account_for_wallet(db,w),amount,Decimal("0.00"),w.balance)
    db.commit()
    result={"transaction_ref":tx.transaction_ref,"status":tx.status,"amount":str(amount),"balance":str(w.balance)}
    finish_idempotency(db,rec,result,201); return result

@router.post("/transfers", status_code=201)
def transfer(data: TransferIn, background_tasks: BackgroundTasks, idempotency_key: str=Header(None, alias="Idempotency-Key"), db: Session=Depends(get_db), user=Depends(current_user)):
    rec=check_idempotency(db,user,idempotency_key,"/transfers",data.model_dump())
    if not isinstance(rec,IdempotencyRecord): return rec
    if not user.pin_hash: raise HTTPException(400,"Set transaction PIN first")
    if user.pin_locked_until and user.pin_locked_until>datetime.utcnow(): raise HTTPException(423,"Payments locked for 30 minutes")
    if not verify_value(data.pin,user.pin_hash):
        user.pin_failures += 1
        if user.pin_failures>=3: user.pin_locked_until=datetime.utcnow()+timedelta(minutes=30); user.pin_failures=0
        db.commit(); raise HTTPException(400,"Incorrect transaction PIN")
    user.pin_failures=0
    sender=wallet_of(db,user)
    receiver_user=db.scalar(select(User).where(User.phone==data.receiver_phone))
    if not receiver_user or receiver_user.id==user.id: raise HTTPException(400,"Receiver not found or self-transfer is not allowed")
    receiver=wallet_of(db,receiver_user)
    if sender.kyc_level=="Minimum" and data.amount>10000: raise HTTPException(400,"Minimum KYC per-day transfer limit is ₹10,000")
    if data.amount>5000 and not data.otp: raise HTTPException(400,"OTP required for transfers above ₹5,000")
    tx=WalletTransaction(transaction_ref="TXN-"+datetime.utcnow().strftime("%Y%m%d")+"-"+uuid4().hex[:6].upper(),type="P2P Transfer",amount=data.amount,status="Success",initiated_by=user.id,description=data.note)
    db.add(tx); db.flush()
    move_between_wallets(db,tx,sender,receiver,data.amount)
    db.commit()
    result={"transaction_ref":tx.transaction_ref,"status":tx.status,"amount":str(data.amount),"sender_balance":str(sender.balance),"receiver":receiver_user.full_name}
    finish_idempotency(db,rec,result,201)
    background_tasks.add_task(send_email,receiver_user.email,"Money received",f"You received ₹{data.amount}.")
    background_tasks.add_task(send_email,user.email,"Money sent",f"You sent ₹{data.amount} to {receiver_user.full_name}.")
    return result

@router.get("/transfers/{transaction_ref}")
def transfer_status(transaction_ref: str, db: Session=Depends(get_db), user=Depends(current_user)):
    tx=db.scalar(select(WalletTransaction).where(WalletTransaction.transaction_ref==transaction_ref,WalletTransaction.initiated_by==user.id))
    if not tx: raise HTTPException(404,"Transaction not found")
    return {"transaction_ref":tx.transaction_ref,"type":tx.type,"amount":str(tx.amount),"status":tx.status}

@router.post("/kyc")
def submit_kyc(data: KYCIn, db: Session=Depends(get_db), user=Depends(current_user)):
    row=KYC(user_id=user.id,pan_number=data.pan_number,aadhaar_last4=data.aadhaar_last4,full_name=data.full_name,date_of_birth=data.date_of_birth.isoformat(),status="Pending")
    db.add(row); db.commit(); return {"kyc_id":row.id,"status":row.status}

@router.get("/kyc")
def get_kyc(db: Session=Depends(get_db), user=Depends(current_user)):
    rows=db.scalars(select(KYC).where(KYC.user_id==user.id)).all()
    return [{"id":r.id,"status":r.status,"rejection_reason":r.rejection_reason} for r in rows]

@router.put("/admin/kyc/{kyc_id}/approve")
def approve_kyc(kyc_id:int, db:Session=Depends(get_db), admin=Depends(require_roles("Admin"))):
    row=db.get(KYC,kyc_id)
    if not row: raise HTTPException(404,"KYC not found")
    row.status="Approved"; wallet=db.scalar(select(Wallet).where(Wallet.owner_id==row.user_id)); wallet.kyc_level="Full"; db.commit()
    return {"message":"KYC approved"}

@router.put("/admin/kyc/{kyc_id}/reject")
def reject_kyc(kyc_id:int, reason:str, db:Session=Depends(get_db), admin=Depends(require_roles("Admin"))):
    if not reason.strip(): raise HTTPException(422,"Rejection reason required")
    row=db.get(KYC,kyc_id)
    if not row: raise HTTPException(404,"KYC not found")
    row.status="Rejected"; row.rejection_reason=reason; db.commit(); return {"message":"KYC rejected"}

@router.put("/admin/wallets/{wallet_id}/freeze")
def freeze_wallet(wallet_id:int, reason:str, db:Session=Depends(get_db), admin=Depends(require_roles("Admin"))):
    w=db.get(Wallet,wallet_id)
    if not w: raise HTTPException(404,"Wallet not found")
    if not reason.strip(): raise HTTPException(422,"Reason required")
    w.status="Frozen"; db.commit(); return {"message":"Wallet frozen","reason":reason}

@router.put("/admin/wallets/{wallet_id}/unfreeze")
def unfreeze_wallet(wallet_id:int, db:Session=Depends(get_db), admin=Depends(require_roles("Admin"))):
    w=db.get(Wallet,wallet_id)
    if not w: raise HTTPException(404,"Wallet not found")
    w.status="Active"; db.commit(); return {"message":"Wallet unfrozen"}
