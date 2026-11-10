from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.routers import auth, wallet, other
from app.database import SessionLocal
from app.services.ledger import system_account
from app.jobs.scheduler import start_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    db=SessionLocal()
    try:
        for kind in ["Bank Settlement","Cashback Pool","Fees","Withdrawal Clearing","Hold"]:
            system_account(db,kind)
        db.commit()
    finally: db.close()
    start_scheduler()
    yield

app=FastAPI(title="Digital Wallet & Payments System",version="1.0.0",lifespan=lifespan,description="Educational wallet backend with a double-entry ledger and simulated payment rails.")
app.include_router(auth.router)
app.include_router(wallet.router)
app.include_router(other.router)

@app.get("/")
def root():
    return {"message":"Digital Wallet API is running","docs":"/docs"}
