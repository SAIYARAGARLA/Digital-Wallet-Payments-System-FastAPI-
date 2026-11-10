from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.database import get_db
from app.models import User, Wallet
from app.schemas import RegisterIn, LoginIn
from app.security import hash_value, verify_value, create_token, current_user
from app.utils.notifications import send_email
import secrets

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    if data.role not in ("User", "Merchant"): raise HTTPException(400, "Public registration supports User or Merchant only")
    if data.role == "Merchant" and (not data.business_name or not data.business_category): raise HTTPException(422, "Merchants must provide business_name and business_category")
    if db.scalar(select(User).where((User.email == data.email) | (User.phone == data.phone))): raise HTTPException(409, "Email or phone already registered")
    user = User(full_name=data.full_name, email=data.email, phone=data.phone, password_hash=hash_value(data.password), role=data.role, business_name=data.business_name, business_category=data.business_category)
    db.add(user); db.flush()
    wallet = Wallet(owner_id=user.id, wallet_number=f"WAL-{1000000+user.id}", balance=0, kyc_level="Minimum", status="Active")
    db.add(wallet); db.commit()
    return {"message":"Registration successful", "user_id":user.id, "wallet_number":wallet.wallet_number}

@router.post("/login")
def login(data: LoginIn, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email))
    if not user or not verify_value(data.password, user.password_hash): raise HTTPException(401, "Invalid email or password")
    if not user.is_active: raise HTTPException(403, "User is inactive")
    background_tasks.add_task(send_email, user.email, "New login alert", "A successful login occurred on your wallet account.")
    return {"access_token":create_token(user.id, user.role), "token_type":"bearer", "expires_in_minutes":30}

@router.get("/me")
def me(user=Depends(current_user)):
    return {"id":user.id, "full_name":user.full_name, "email":user.email, "phone":user.phone, "role":user.role}
