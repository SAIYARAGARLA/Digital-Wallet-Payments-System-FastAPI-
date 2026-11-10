from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.database import get_db
from app.models import *
from app.security import current_user, require_roles
from app.schemas import BankAccountIn, MoneyRequestIn
from app.routers.wallet import wallet_of, check_idempotency, finish_idempotency
from app.services.ledger import system_account, account_for_wallet, post_pair, move_between_wallets
import qrcode, os, csv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

router=APIRouter(tags=["Payments, Banking, Requests & Reports"])

@router.get("/merchants/me/qr")
def merchant_qr(db:Session=Depends(get_db), user=Depends(require_roles("Merchant"))):
    code=f"MER-{10000+user.id}"
    os.makedirs("statements",exist_ok=True); path=f"statements/{code}.png"; qrcode.make(code).save(path)
    return {"merchant_code":code,"qr_image":path}

@router.post("/payments/merchant")
def merchant_payment(merchant_code:str, amount:Decimal, pin:str, idempotency_key:str=Header(None,alias="Idempotency-Key"), db:Session=Depends(get_db), user=Depends(current_user)):
    body={"merchant_code":merchant_code,"amount":str(amount),"pin":pin}
    rec=check_idempotency(db,user,idempotency_key,"/payments/merchant",body)
    if not isinstance(rec,IdempotencyRecord): return rec
    merchant_id=int(merchant_code.replace("MER-",""))-10000 if merchant_code.startswith("MER-") else -1
    merchant=db.get(User,merchant_id)
    if not merchant or merchant.role!="Merchant": raise HTTPException(404,"Merchant not found")
    if not user.pin_hash: raise HTTPException(400,"Set PIN first")
    from app.security import verify_value
    if not verify_value(pin,user.pin_hash): raise HTTPException(400,"Incorrect PIN")
    sender=wallet_of(db,user); receiver=wallet_of(db,merchant); fee=(amount*Decimal("0.01")).quantize(Decimal("0.01")); net=amount-fee
    tx=WalletTransaction(transaction_ref="TXN-"+uuid4().hex[:12].upper(),type="Merchant Payment",amount=amount,status="Success",initiated_by=user.id,description=f"Payment to {merchant.business_name}")
    db.add(tx); db.flush()
    if sender.balance<amount: raise HTTPException(400,"Insufficient balance")
    sender.balance-=amount; receiver.balance+=net
    post_pair(db,tx,account_for_wallet(db,sender),account_for_wallet(db,receiver),net,sender.balance,receiver.balance)
    if fee>0: post_pair(db,tx,account_for_wallet(db,sender),system_account(db,"Fees"),fee,sender.balance,Decimal("0.00"))
    db.commit()
    result={"transaction_ref":tx.transaction_ref,"status":"Success","merchant":merchant.business_name,"fee":str(fee),"balance":str(sender.balance)}
    finish_idempotency(db,rec,result,201); return result

@router.get("/merchants/me/payments")
def merchant_payments(skip:int=0,limit:int=20,db:Session=Depends(get_db),user=Depends(require_roles("Merchant"))):
    return [{"transaction_ref":t.transaction_ref,"amount":str(t.amount),"status":t.status,"created_at":t.created_at.isoformat()} for t in db.scalars(select(WalletTransaction).where(WalletTransaction.description.like(f"%{user.business_name}%")).offset(skip).limit(limit)).all()]

@router.post("/money-requests",status_code=201)
def create_request(data:MoneyRequestIn,db:Session=Depends(get_db),user=Depends(current_user)):
    payer=db.scalar(select(User).where(User.phone==data.payer_phone))
    if not payer: raise HTTPException(404,"Payer not found")
    pending=db.scalar(select(func.count(MoneyRequest.id)).where(MoneyRequest.requester_id==user.id,MoneyRequest.payer_id==payer.id,MoneyRequest.status=="Pending"))
    if pending>=5: raise HTTPException(400,"Maximum 5 pending requests to the same person")
    row=MoneyRequest(requester_id=user.id,payer_id=payer.id,amount=data.amount,note=data.note,status="Pending",expires_at=datetime.utcnow()+timedelta(hours=48))
    db.add(row);db.commit();return {"request_id":row.id,"status":row.status}

@router.get("/money-requests")
def list_requests(skip:int=0,limit:int=20,db:Session=Depends(get_db),user=Depends(current_user)):
    rows=db.scalars(select(MoneyRequest).where((MoneyRequest.requester_id==user.id)|(MoneyRequest.payer_id==user.id)).order_by(MoneyRequest.created_at.desc()).offset(skip).limit(limit)).all()
    return [{"id":r.id,"requester_id":r.requester_id,"payer_id":r.payer_id,"amount":str(r.amount),"status":r.status,"expires_at":r.expires_at.isoformat()} for r in rows]

@router.put("/money-requests/{request_id}/decline")
def decline_request(request_id:int,db:Session=Depends(get_db),user=Depends(current_user)):
    r=db.get(MoneyRequest,request_id)
    if not r or r.payer_id!=user.id: raise HTTPException(404,"Request not found")
    r.status="Declined";db.commit();return {"status":r.status}

@router.put("/money-requests/{request_id}/cancel")
def cancel_request(request_id:int,db:Session=Depends(get_db),user=Depends(current_user)):
    r=db.get(MoneyRequest,request_id)
    if not r or r.requester_id!=user.id: raise HTTPException(404,"Request not found")
    r.status="Cancelled";db.commit();return {"status":r.status}

@router.post("/bank-accounts",status_code=201)
def add_bank(data:BankAccountIn,db:Session=Depends(get_db),user=Depends(current_user)):
    count=db.scalar(select(func.count(BankAccount.id)).where(BankAccount.user_id==user.id))
    if count>=3: raise HTTPException(400,"Maximum 3 bank accounts")
    row=BankAccount(user_id=user.id,**data.model_dump());db.add(row);db.commit()
    return {"id":row.id,"account_number":"X"*max(0,len(row.account_number)-4)+row.account_number[-4:],"ifsc_code":row.ifsc_code}

@router.get("/bank-accounts")
def banks(db:Session=Depends(get_db),user=Depends(current_user)):
    rows=db.scalars(select(BankAccount).where(BankAccount.user_id==user.id)).all()
    return [{"id":r.id,"bank_name":r.bank_name,"account_number":"X"*(len(r.account_number)-4)+r.account_number[-4:],"ifsc_code":r.ifsc_code,"is_primary":r.is_primary} for r in rows]

@router.delete("/bank-accounts/{bank_account_id}")
def delete_bank(bank_account_id:int,db:Session=Depends(get_db),user=Depends(current_user)):
    row=db.get(BankAccount,bank_account_id)
    if not row or row.user_id!=user.id: raise HTTPException(404,"Bank account not found")
    db.delete(row);db.commit();return {"message":"Bank account deleted"}

@router.post("/withdrawals",status_code=201)
def withdraw(amount:Decimal,bank_account_id:int,pin:str,idempotency_key:str=Header(None,alias="Idempotency-Key"),db:Session=Depends(get_db),user=Depends(current_user)):
    body={"amount":str(amount),"bank_account_id":bank_account_id,"pin":pin}; rec=check_idempotency(db,user,idempotency_key,"/withdrawals",body)
    if not isinstance(rec,IdempotencyRecord): return rec
    w=wallet_of(db,user); bank=db.get(BankAccount,bank_account_id)
    if not bank or bank.user_id!=user.id: raise HTTPException(404,"Bank account not found")
    if user.role!="Merchant" and w.kyc_level!="Full": raise HTTPException(403,"Full KYC required")
    if amount<100: raise HTTPException(400,"Minimum withdrawal is ₹100")
    from app.security import verify_value
    if not user.pin_hash or not verify_value(pin,user.pin_hash): raise HTTPException(400,"Incorrect PIN")
    if w.balance<amount: raise HTTPException(400,"Insufficient balance")
    tx=WalletTransaction(transaction_ref="TXN-"+uuid4().hex[:12].upper(),type="Withdrawal",amount=amount,status="Pending",initiated_by=user.id,description="Bank withdrawal")
    db.add(tx);db.flush();w.balance-=amount
    post_pair(db,tx,account_for_wallet(db,w),system_account(db,"Withdrawal Clearing"),amount,w.balance,Decimal("0.00"))
    row=Withdrawal(transaction_id=tx.id,bank_account_id=bank.id,amount=amount,status="Pending");db.add(row);db.commit()
    result={"withdrawal_id":row.id,"transaction_ref":tx.transaction_ref,"status":"Pending","balance":str(w.balance)}
    finish_idempotency(db,rec,result,201);return result

@router.get("/withdrawals")
def withdrawals(skip:int=0,limit:int=20,db:Session=Depends(get_db),user=Depends(current_user)):
    rows=db.scalars(select(Withdrawal).join(WalletTransaction).where(WalletTransaction.initiated_by==user.id).offset(skip).limit(limit)).all()
    return [{"id":r.id,"amount":str(r.amount),"status":r.status} for r in rows]

@router.get("/transactions")
def transactions(skip:int=0,limit:int=20,type:str|None=None,status:str|None=None,min_amount:Decimal|None=None,max_amount:Decimal|None=None,db:Session=Depends(get_db),user=Depends(current_user)):
    q=select(WalletTransaction).where(WalletTransaction.initiated_by==user.id)
    if type:q=q.where(WalletTransaction.type==type)
    if status:q=q.where(WalletTransaction.status==status)
    if min_amount is not None:q=q.where(WalletTransaction.amount>=min_amount)
    if max_amount is not None:q=q.where(WalletTransaction.amount<=max_amount)
    rows=db.scalars(q.order_by(WalletTransaction.created_at.desc()).offset(skip).limit(min(limit,100))).all()
    return [{"transaction_ref":r.transaction_ref,"type":r.type,"amount":str(r.amount),"status":r.status,"description":r.description,"created_at":r.created_at.isoformat()} for r in rows]

@router.get("/transactions/{transaction_ref}")
def transaction_detail(transaction_ref:str,db:Session=Depends(get_db),user=Depends(current_user)):
    tx=db.scalar(select(WalletTransaction).where(WalletTransaction.transaction_ref==transaction_ref,WalletTransaction.initiated_by==user.id))
    if not tx: raise HTTPException(404,"Transaction not found")
    return {"transaction_ref":tx.transaction_ref,"type":tx.type,"amount":str(tx.amount),"status":tx.status,"description":tx.description}

@router.get("/statements")
def statement(month:str,format:str="csv",db:Session=Depends(get_db),user=Depends(current_user)):
    try: year,mon=map(int,month.split("-")); start=datetime(year,mon,1); end=datetime(year+1,1,1) if mon==12 else datetime(year,mon+1,1)
    except Exception: raise HTTPException(422,"month must be YYYY-MM")
    rows=db.scalars(select(WalletTransaction).where(WalletTransaction.initiated_by==user.id,WalletTransaction.created_at>=start,WalletTransaction.created_at<end).order_by(WalletTransaction.created_at)).all()
    os.makedirs("statements",exist_ok=True); path=f"statements/{user.id}_{month}.{format}"
    if format=="csv":
        with open(path,"w",newline="",encoding="utf-8") as f:
            writer=csv.writer(f);writer.writerow(["Reference","Date","Type","Amount","Status"])
            for r in rows:writer.writerow([r.transaction_ref,r.created_at,r.type,r.amount,r.status])
        from fastapi.responses import FileResponse
        return FileResponse(path,media_type="text/csv",filename=f"statement-{month}.csv")
    if format=="pdf":
        c=canvas.Canvas(path,pagesize=A4);c.drawString(50,800,f"Wallet statement - {user.full_name} - {month}")
        y=770
        for r in rows:
            c.drawString(50,y,f"{r.created_at:%Y-%m-%d} | {r.type} | INR {r.amount} | {r.status}");y-=18
            if y<50:c.showPage();y=800
        c.save()
        from fastapi.responses import FileResponse
        return FileResponse(path,media_type="application/pdf",filename=f"statement-{month}.pdf")
    raise HTTPException(422,"format must be pdf or csv")

@router.get("/admin/flagged-transactions")
def flagged(skip:int=0,limit:int=20,db:Session=Depends(get_db),admin=Depends(require_roles("Admin"))):
    rows=db.scalars(select(WalletTransaction).where(WalletTransaction.is_flagged==True).offset(skip).limit(limit)).all()
    return [{"transaction_ref":r.transaction_ref,"amount":str(r.amount),"status":r.status} for r in rows]

@router.get("/reports/admin-dashboard")
def admin_dashboard(db:Session=Depends(get_db),admin=Depends(require_roles("Admin"))):
    wallets=db.scalars(select(Wallet)).all(); txs=db.scalars(select(WalletTransaction)).all()
    return {"total_wallet_balance":str(sum((w.balance for w in wallets),Decimal("0.00"))),"transaction_count":len(txs),"failed_transactions":sum(t.status=="Failed" for t in txs),"on_hold":sum(t.status=="On Hold" for t in txs)}

@router.get("/reports/merchant-sales")
def merchant_sales(db:Session=Depends(get_db),user=Depends(require_roles("Merchant"))):
    rows=db.scalars(select(WalletTransaction).where(WalletTransaction.description.like(f"%{user.business_name}%"))).all()
    return {"payment_count":len(rows),"gross_sales":str(sum((r.amount for r in rows),Decimal("0.00")))}

@router.get("/reports/my-spending")
def my_spending(db:Session=Depends(get_db),user=Depends(current_user)):
    rows=db.scalars(select(WalletTransaction).where(WalletTransaction.initiated_by==user.id)).all()
    return {"total_spending":str(sum((r.amount for r in rows if r.type in ("P2P Transfer","Merchant Payment","Withdrawal")),Decimal("0.00"))),"transaction_count":len(rows)}
