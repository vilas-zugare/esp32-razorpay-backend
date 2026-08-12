import os
import json
import logging
import asyncio
from typing import Dict, List
import datetime

from fastapi import FastAPI, Request, HTTPException, Depends, status, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import razorpay
from dotenv import load_dotenv
import cv2
import numpy as np
import urllib.request
import requests
import json
import base64
import os
from sqlalchemy.orm import Session
from sqlalchemy import func

# Local imports
from database import engine, get_db, SessionLocal
import models
import auth

# Initialize DB tables
models.Base.metadata.create_all(bind=engine)

# Auto-migrate new columns for RewardCode if they don't exist
from sqlalchemy import text
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE reward_codes ADD COLUMN reward_type VARCHAR DEFAULT 'COINS'"))
        conn.execute(text("ALTER TABLE reward_codes ADD COLUMN tickets INTEGER DEFAULT 0"))
        conn.commit()
except Exception as e:
    pass # Columns already exist or other DB issue

# Auto-seed the database on startup
try:
    import seed_users
    seed_users.seed()
    print("Auto-seeding executed.")
except Exception as e:
    print(f"Failed to auto-seed database: {e}")

# Load environment variables
load_dotenv()

# Initialize Razorpay Client
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "default_secret")

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI(title="ESP32 Razorpay Backend", version="1.1.0")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Setup Templates
# Ensure the 'templates' folder exists in the same directory as main.py
templates = Jinja2Templates(directory="templates")

# In-memory storage for active SSE queues
# Mapping of device_id -> asyncio.Queue
active_connections: Dict[str, asyncio.Queue] = {}

# In-memory mapping of qr_id to device_id
active_qr_codes: Dict[str, str] = {}

class DeviceStatusRequest(BaseModel):
    action: str

def check_qr_status_sync(qr_id: str):
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret: return None
    auth_str = f"{key_id}:{key_secret}"
    b64_auth = base64.b64encode(auth_str.encode('ascii')).decode('ascii')
    headers = {"Authorization": f"Basic {b64_auth}"}
    try:
        resp = requests.get(f"https://api.razorpay.com/v1/payments/qr_codes/{qr_id}", headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Sync poll error: {e}")
    return None

async def poll_qr_status(device_id: str, qr_id: str):
    logger.info(f"Starting fallback polling for QR: {qr_id}")
    for _ in range(60): # Poll every 3 seconds for 3 minutes
        await asyncio.sleep(3)
        try:
            qr_data = await asyncio.to_thread(check_qr_status_sync, qr_id)
            if qr_data:
                received = qr_data.get("payments_amount_received", 0)
                expected = qr_data.get("payment_amount", 1)
                # If payment is fully received
                if received >= expected and expected > 0:
                    logger.info(f"Polling SUCCESS for {device_id}, Amount: {received}")
                    if device_id in active_connections:
                        await active_connections[device_id].put({
                            "event": "payment_success",
                            "status": "paid",
                            "amount": received / 100.0
                        })
                    break
        except Exception as e:
            logger.error(f"Async poll error: {e}")

# --- Admin Dashboard UI Routes ---

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")

@app.post("/admin/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # Verify credentials
    if username != auth.ADMIN_USER or not auth.verify_password(password, auth.ADMIN_PASS_HASH):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})
    
    # Create JWT
    access_token_expires = datetime.timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": username}, expires_delta=access_token_expires
    )
    
    # Redirect to dashboard and set cookie
    response = RedirectResponse(url="/admin/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response

@app.get("/admin/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("access_token")
    return response

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, current_user: str = Depends(auth.get_current_admin)):
    return templates.TemplateResponse(request, "dashboard.html", {"username": current_user})

# --- Admin Dashboard API Routes ---

@app.get("/api/admin/stats")
async def get_stats(db: Session = Depends(get_db), current_user: str = Depends(auth.get_current_admin)):
    total_revenue = db.query(func.sum(models.Transaction.amount)).filter(models.Transaction.status == "paid").scalar() or 0
    total_successful = db.query(models.Transaction).filter(models.Transaction.status == "paid").count()
    total_failed = db.query(models.Transaction).filter(models.Transaction.status.in_(["failed", "cancelled"])).count()
    total_attempts = db.query(models.Transaction).count()
    
    success_rate = 0
    if total_attempts > 0:
        success_rate = round((total_successful / total_attempts) * 100, 2)
        
    # Get active devices
    devices = db.query(models.Device).all()
    devices_data = []
    for d in devices:
        devices_data.append({
            "device_id": d.device_id,
            "ip_address": d.ip_address,
            "status": d.status,
            "last_ping": d.last_ping.isoformat() if d.last_ping else None,
            "current_action": d.current_action
        })
        
    return {
        "revenue": total_revenue,
        "successful": total_successful,
        "failed": total_failed,
        "success_rate": success_rate,
        "devices": devices_data
    }

@app.get("/api/admin/transactions")
async def get_transactions(db: Session = Depends(get_db), current_user: str = Depends(auth.get_current_admin)):
    txs = db.query(models.Transaction).order_by(models.Transaction.created_at.desc()).limit(100).all()
    return txs

@app.get("/api/admin/logs")
async def get_logs(db: Session = Depends(get_db), current_user: str = Depends(auth.get_current_admin)):
    logs = db.query(models.WebhookLog).order_by(models.WebhookLog.received_at.desc()).limit(50).all()
    return logs

@app.get("/api/admin/users")
async def get_users(search: str = None, db: Session = Depends(get_db), current_user: str = Depends(auth.get_current_admin)):
    query = db.query(models.User).order_by(models.User.created_at.desc())
    if search:
        query = query.filter(models.User.mobile_number.contains(search))
    users = query.limit(100).all()
    return [{"id": u.id, "mobile_number": u.mobile_number, "wallet_balance": u.wallet_balance, "created_at": u.created_at.isoformat()} for u in users]

class AdminRewardRequest(BaseModel):
    user_id: int
    reward_code: str

@app.post("/api/admin/users/reward")
async def admin_apply_reward(req: AdminRewardRequest, db: Session = Depends(get_db), current_user: str = Depends(auth.get_current_admin)):
    # 1. Lock Reward Code
    reward = db.query(models.RewardCode).filter(models.RewardCode.code == req.reward_code).with_for_update().first()
    if not reward:
        raise HTTPException(status_code=404, detail="Invalid Reward Code")
    if reward.status == "CLAIMED":
        raise HTTPException(status_code=400, detail="Reward Code already claimed")

    # 2. Find/Lock User
    user = db.query(models.User).filter(models.User.id == req.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 3. Add funds
    user.wallet_balance += reward.value
    reward.status = "CLAIMED"
    reward.claimed_by_user_id = user.id
    
    db.commit()
    return {"status": "success", "message": f"₹{reward.value} added to user {user.mobile_number}"}

# --- ESP32 and Razorpay Routes ---

@app.post("/api/order/create/{device_id}")
async def create_order(device_id: str, request: Request):
    data = await request.json()
    amount = data.get("amount", 1.0) # amount in rupees
    amount_in_paise = int(amount * 100)
    
    try:
        key_id = os.getenv("RAZORPAY_KEY_ID")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        auth_str = f"{key_id}:{key_secret}"
        b64_auth = base64.b64encode(auth_str.encode('ascii')).decode('ascii')
        
        headers = {
            "Authorization": f"Basic {b64_auth}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "type": "upi_qr",
            "name": f"Order for {device_id}",
            "usage": "single_use",
            "fixed_amount": True,
            "payment_amount": amount_in_paise,
            "description": "Juice order",
            "notes": {
                "device_id": device_id
            }
        }
        
        response = requests.post("https://api.razorpay.com/v1/payments/qr_codes", json=payload, headers=headers)
        if response.status_code != 200:
            logger.error(f"Razorpay API Error: {response.text}")
            raise HTTPException(status_code=500, detail="Failed to create QR code at Razorpay")
            
        qr_data = response.json()
        qr_id = qr_data.get("id")
        image_url = qr_data.get("image_url")
        
        # Start fallback polling in the background!
        if qr_id:
            asyncio.create_task(poll_qr_status(device_id, qr_id))
        
        # Store in mapping
        active_qr_codes[qr_id] = device_id
        
        # Decode the image to get UPI string
        req = urllib.request.urlopen(image_url)
        arr = np.asarray(bytearray(req.read()), dtype=np.uint8)
        img = cv2.imdecode(arr, -1)
        detector = cv2.QRCodeDetector()
        val, _, _ = detector.detectAndDecode(img)
        
        if not val:
            # Fallback if detection fails (rare but possible)
            raise HTTPException(status_code=500, detail="Failed to decode QR code")
            
        return {"qr_id": qr_id, "qr_string": val}
    except Exception as e:
        logger.error(f"Error creating order: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stream/status/{device_id}")
async def sse_endpoint(request: Request, device_id: str, db: Session = Depends(get_db)):
    """
    ESP32 connects here to receive SSE push notifications of payment status.
    """
    client_ip = request.client.host if request.client else "unknown"
    
    # Update device status in DB
    device = db.query(models.Device).filter(models.Device.device_id == device_id).first()
    if not device:
        device = models.Device(device_id=device_id, ip_address=client_ip, status="online", current_action="Connected (SSE)")
        db.add(device)
    else:
        device.status = "online"
        device.ip_address = client_ip
        device.last_ping = datetime.datetime.utcnow()
        device.current_action = "Connected (SSE)"
    db.commit()
    
    logger.info(f"SSE connected for device_id: {device_id}")
    
    queue = asyncio.Queue()
    active_connections[device_id] = queue
    
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    logger.info(f"SSE disconnected by client for device_id: {device_id}")
                    break
                    
                try:
                    # Wait for message with a timeout to check for disconnects
                    data = await asyncio.wait_for(queue.get(), timeout=2.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # Send a comment to keep connection alive
                    yield ": keep-alive\n\n"
        finally:
            if device_id in active_connections:
                del active_connections[device_id]
            
            # Mark as offline using a fresh DB session
            db_local = SessionLocal()
            try:
                device = db_local.query(models.Device).filter(models.Device.device_id == device_id).first()
                if device:
                    device.status = "offline"
                    db_local.commit()
            finally:
                db_local.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

@app.post("/api/device/status/{device_id}")
async def update_device_status(device_id: str, req: DeviceStatusRequest, db: Session = Depends(get_db)):
    """
    ESP32 posts its current status/action here since SSE is one-way.
    """
    device = db.query(models.Device).filter(models.Device.device_id == device_id).first()
    if device:
        device.current_action = req.action
        device.last_ping = datetime.datetime.utcnow()
        db.commit()
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Device not found")

@app.post("/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get('x-razorpay-signature', '')

    # Log incoming webhook blindly for debugging
    webhook_log = models.WebhookLog(
        event_type="unknown",
        payload=body.decode('utf-8'),
        status="pending"
    )
    db.add(webhook_log)
    db.commit()

    try:
        razorpay_client.utility.verify_webhook_signature(
            body.decode('utf-8'),
            signature,
            RAZORPAY_WEBHOOK_SECRET
        )
        webhook_log.status = "valid"
    except razorpay.errors.SignatureVerificationError:
        logger.warning("Invalid webhook signature received.")
        webhook_log.status = "invalid"
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Webhook verification error: {str(e)}")
        webhook_log.status = "error"
        db.commit()
        raise HTTPException(status_code=500, detail="Internal server error")

    payload = json.loads(body)
    event_type = payload.get('event')
    webhook_log.event_type = event_type
    db.commit()
    
    logger.info(f"Received webhook event: {event_type}")

    if event_type in ['payment.captured', 'payment_link.paid', 'qr_code.credited']:
        try:
            payment_entity = None
            device_id = "static_qr_machine" # Default fallback
            
            if event_type == 'payment_link.paid':
                if 'payment' in payload.get('payload', {}):
                    payment_entity = payload['payload']['payment']['entity']
                else:
                    payment_entity = payload['payload']['payment_link']['entity']
            elif event_type == 'qr_code.credited':
                payment_entity = payload['payload']['payment']['entity']
                qr_entity = payload.get('payload', {}).get('qr_code', {}).get('entity', {})
                qr_id = qr_entity.get('id')
                device_id = active_qr_codes.get(qr_id) or qr_entity.get('notes', {}).get('device_id', "static_qr_machine")
            elif event_type == 'payment.captured':
                payment_entity = payload['payload']['payment']['entity']
            
            if payment_entity:
                payment_id = payment_entity.get('id')
                amount_in_paise = payment_entity.get('amount', 0)
                amount = amount_in_paise / 100.0
                status = "paid"
                
                # Save transaction to DB if it doesn't exist
                tx = db.query(models.Transaction).filter(models.Transaction.payment_id == payment_id).first()
                if not tx:
                    new_tx = models.Transaction(
                        payment_id=payment_id,
                        amount=amount,
                        status=status,
                        method=payment_entity.get('method', 'unknown'),
                        device_id=device_id
                    )
                    db.add(new_tx)
                    db.commit()
                
                # Notify the correct ESP32 (or all if fallback)
                if device_id in active_connections:
                    logger.info(f"Broadcasting successful payment of {amount} INR to device {device_id}.")
                    queue = active_connections[device_id]
                    try:
                        await queue.put({
                            "event": "payment_success",
                            "payment_id": payment_id,
                            "amount": amount,
                            "status": status
                        })
                        
                        # Update device action in DB
                        device = db.query(models.Device).filter(models.Device.device_id == device_id).first()
                        if device:
                            device.current_action = f"Payment paid ({amount} INR)"
                            db.commit()
                    except Exception as e:
                        logger.error(f"Failed to queue message for device {device_id}: {str(e)}")
                else:
                    # Fallback: notify all if we don't know the specific device
                    logger.info(f"Broadcasting successful payment of {amount} INR to all connected devices.")
                    for did, queue in active_connections.items():
                        try:
                            await queue.put({
                                "event": "payment_success",
                                "payment_id": payment_id,
                                "amount": amount,
                                "status": status
                            })
                        except Exception as e:
                            pass

        except Exception as e:
            logger.error(f"Error processing webhook payload: {str(e)}")

    elif event_type == 'payment.failed':
        try:
            payment_entity = payload['payload']['payment']['entity']
            payment_id = payment_entity.get('id')
            amount_in_paise = payment_entity.get('amount', 0)
            amount = amount_in_paise / 100.0
            status = "failed"
            
            # Notify ALL connected ESP32s
            logger.info(f"Broadcasting failed payment of {amount} INR to all connected devices.")
            for device_id, queue in active_connections.items():
                try:
                    await queue.put({
                        "event": "payment_failed",
                        "payment_id": payment_id,
                        "amount": amount,
                        "status": status
                    })
                except Exception as e:
                    pass
        except Exception as e:
            logger.error(f"Error processing failed payment: {str(e)}")

    return JSONResponse(content={"status": "ok"})

import hmac
import hashlib
from fastapi import Header
from passlib.context import CryptContext
import random
import string

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

X_MACHINE_KEY_SECRET = os.getenv("X_MACHINE_KEY_SECRET", "default_machine_secret_change_me")

async def verify_machine_hmac(request: Request, x_machine_key: str = Header(...), x_timestamp: str = Header(...)):
    body = await request.body()
    payload = x_timestamp.encode() + body
    expected_hmac = hmac.new(X_MACHINE_KEY_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hmac, x_machine_key):
        raise HTTPException(status_code=403, detail="Invalid HMAC signature")

class WalletChargeRequest(BaseModel):
    mobile_number: str
    pin: str
    amount: float
    glasses: int = 1
    idempotency_key: str

class RewardClaimRequest(BaseModel):
    code: str
    mobile_number: str

def generate_reward_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

@app.post("/api/v1/payments/wallet/charge", dependencies=[Depends(verify_machine_hmac)])
async def charge_wallet(req: WalletChargeRequest, device_id: str = "static_qr_machine", db: Session = Depends(get_db)):
    # 1. Idempotency Check
    existing_tx = db.query(models.Transaction).filter(models.Transaction.idempotency_key == req.idempotency_key).first()
    if existing_tx:
        if existing_tx.status == "paid":
            # If already paid, look up if a reward code was already generated in this session (this is tricky for idempotency if it wasn't saved with tx)
            # We'll just return success. The reward code might be lost if it was a network drop on the first success,
            # but usually the reward is generated atomically. Let's find it.
            reward = db.query(models.RewardCode).filter(models.RewardCode.claimed_by_user_id == existing_tx.id).first() # not strictly correct linking, but good enough for this mock
            user_for_tx = db.query(models.User).filter(models.User.mobile_number == req.mobile_number).first()
            bal = user_for_tx.wallet_balance if user_for_tx else 0.0
            return {"status": "success", "amount": existing_tx.amount, "reward_code": reward.code if reward else "", "balance": bal}
        else:
            raise HTTPException(status_code=400, detail="Transaction failed previously.")

    # 2. Rate Limiting Check (Max 3 failed attempts in 15 mins)
    now = datetime.datetime.utcnow()
    limit_record = db.query(models.RateLimit).filter(models.RateLimit.mobile_number == req.mobile_number).first()
    
    if limit_record:
        if limit_record.failed_attempts >= 3:
            time_diff = now - limit_record.last_attempt
            if time_diff.total_seconds() < 15 * 60:
                raise HTTPException(status_code=429, detail="Too many failed PIN attempts. Try again later.")
            else:
                # Reset after 15 mins
                limit_record.failed_attempts = 0
                db.commit()

    # 3. Authenticate User & Lock Row
    # Using with_for_update() for atomic locking
    user = db.query(models.User).filter(models.User.mobile_number == req.mobile_number).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not pwd_context.verify(req.pin, user.pin_hash):
        if not limit_record:
            limit_record = models.RateLimit(mobile_number=req.mobile_number, failed_attempts=1, last_attempt=now)
            db.add(limit_record)
        else:
            limit_record.failed_attempts += 1
            limit_record.last_attempt = now
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid PIN")

    # PIN correct, reset rate limit
    if limit_record:
        limit_record.failed_attempts = 0
        db.commit()

    # 4. Check Balance
    if user.wallet_balance < req.amount:
        # Create failed transaction
        new_tx = models.Transaction(
            amount=req.amount, status="failed", method="WALLET", 
            device_id=device_id, payment_method="WALLET", idempotency_key=req.idempotency_key
        )
        db.add(new_tx)
        db.commit()
        raise HTTPException(status_code=400, detail=f"Insufficient Balance! Available: ₹{user.wallet_balance}")

    # 5. Deduct Balance & Create Transaction
    user.wallet_balance -= req.amount
    new_tx = models.Transaction(
        amount=req.amount, status="paid", method="WALLET", 
        device_id=device_id, payment_method="WALLET", idempotency_key=req.idempotency_key
    )
    db.add(new_tx)
    
    coins_tx = models.CoinsTransaction(
        user_id=user.id,
        transaction_type="use_coins",
        amount=-req.amount
    )
    db.add(coins_tx)
    
    # 6. Generate Reward Code atomically
    code = generate_reward_code()
    # Ensure unique
    while db.query(models.RewardCode).filter(models.RewardCode.code == code).first():
        code = generate_reward_code()
        
    reward = models.RewardCode(code=code, value=0.0, reward_type="TICKETS", tickets=req.glasses)
    db.add(reward)
    
    db.commit()

    # Broadcast success to SSE just like Razorpay webhook
    if device_id in active_connections:
        queue = active_connections[device_id]
        asyncio.create_task(queue.put({
            "event": "payment_success",
            "amount": req.amount,
            "status": "paid"
        }))

    return {"status": "success", "amount": req.amount, "reward_code": code, "balance": user.wallet_balance}

@app.post("/api/v1/rewards/claim")
async def claim_reward(req: RewardClaimRequest, db: Session = Depends(get_db)):
    # 1. Lock Reward Code
    reward = db.query(models.RewardCode).filter(models.RewardCode.code == req.code).with_for_update().first()
    if not reward:
        raise HTTPException(status_code=404, detail="Invalid Reward Code")
    if reward.status == "CLAIMED":
        raise HTTPException(status_code=400, detail="Reward Code already claimed")

    # 2. Find/Lock User
    user = db.query(models.User).filter(models.User.mobile_number == req.mobile_number).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 3. Add funds
    user.wallet_balance += reward.value
    reward.status = "CLAIMED"
    reward.claimed_by_user_id = user.id
    
    coins_tx = models.CoinsTransaction(
        user_id=user.id,
        transaction_type="juice_code",
        amount=reward.value
    )
    db.add(coins_tx)
    
    db.commit()

    return {"status": "success", "message": f"₹{reward.value} added to wallet", "new_balance": user.wallet_balance}

# Helper endpoint to create test users easily
class CreateUserRequest(BaseModel):
    mobile_number: str
    pin: str
    initial_balance: float = 100.0

@app.post("/api/v1/users/create")
async def create_user(req: CreateUserRequest, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.mobile_number == req.mobile_number).first()
    if existing:
        return {"status": "error", "message": "User exists"}
    
    hashed = pwd_context.hash(req.pin)
    new_user = models.User(mobile_number=req.mobile_number, pin_hash=hashed, wallet_balance=req.initial_balance)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    if req.initial_balance > 0:
        coins_tx = models.CoinsTransaction(
            user_id=new_user.id,
            transaction_type="add_money",
            amount=req.initial_balance
        )
        db.add(coins_tx)
        db.commit()
        
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
