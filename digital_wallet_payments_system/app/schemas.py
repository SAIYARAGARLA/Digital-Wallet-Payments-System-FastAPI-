from datetime import date
from decimal import Decimal
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator
import re

class RegisterIn(BaseModel):
    full_name: str
    email: EmailStr
    phone: str = Field(pattern=r"^\d{10}$")
    password: str = Field(min_length=8)
    role: str = "User"
    business_name: str | None = None
    business_category: str | None = None

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class AddMoneyIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    source: str
    simulate_status: str = "Success"

class TransferIn(BaseModel):
    receiver_phone: str = Field(pattern=r"^\d{10}$")
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    note: str = ""
    pin: str
    otp: str | None = None

class PinIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4}$")

class KYCIn(BaseModel):
    pan_number: str = Field(pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    aadhaar_last4: str = Field(pattern=r"^\d{4}$")
    full_name: str
    date_of_birth: date

class BankAccountIn(BaseModel):
    account_holder_name: str
    account_number: str = Field(pattern=r"^\d{9,18}$")
    ifsc_code: str = Field(pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    bank_name: str

class MoneyRequestIn(BaseModel):
    payer_phone: str = Field(pattern=r"^\d{10}$")
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    note: str = ""
