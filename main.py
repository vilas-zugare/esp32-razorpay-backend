import os
import json
import logging
from typing import Dict, List
import datetime

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends, status, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import razorpay
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import func

# Local imports
from database import engine, get_db
import models
import auth

# Initialize DB tables
models.Base.metadata.create_all(bind=engine)

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

# In-memory storage for active WebSocket connections
# Mapping of device_id (or reference_id) -> WebSocket
active_connections: Dict[str, WebSocket] = {}



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

# --- ESP32 and Razorpay Routes ---



@app.websocket("/ws/status/{device_id}")
async def websocket_endpoint(websocket: WebSocket, device_id: str, db: Session = Depends(get_db)):
    """
    ESP32 connects here to receive push notifications of payment status,
    and simultaneously registers itself as ONLINE.
    """
    await websocket.accept()
    active_connections[device_id] = websocket
    
    client_ip = websocket.client.host if websocket.client else "unknown"
    
    # Update device status in DB
    device = db.query(models.Device).filter(models.Device.device_id == device_id).first()
    if not device:
        device = models.Device(device_id=device_id, ip_address=client_ip, status="online", current_action="Connected")
        db.add(device)
    else:
        device.status = "online"
        device.ip_address = client_ip
        device.last_ping = datetime.datetime.utcnow()
        device.current_action = "Connected"
    db.commit()
    
    logger.info(f"WebSocket connected for device_id: {device_id}")
    try:
        while True:
            # Simple heartbeat loop
            data = await websocket.receive_text()
            logger.info(f"Received from ESP32 ({device_id}): {data}")
            
            # Update last_ping
            device = db.query(models.Device).filter(models.Device.device_id == device_id).first()
            if device:
                device.last_ping = datetime.datetime.utcnow()
                
                # Optional: ESP32 can send its current screen state in the heartbeat
                try:
                    payload = json.loads(data)
                    if "action" in payload:
                        device.current_action = payload["action"]
                except json.JSONDecodeError:
                    pass
                db.commit()
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for device_id: {device_id}")
        if device_id in active_connections:
            del active_connections[device_id]
        
        # Mark as offline
        device = db.query(models.Device).filter(models.Device.device_id == device_id).first()
        if device:
            device.status = "offline"
            db.commit()

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
            # Extract payment entity
            payment_entity = None
            if event_type == 'payment_link.paid':
                if 'payment' in payload.get('payload', {}):
                    payment_entity = payload['payload']['payment']['entity']
                else:
                    payment_entity = payload['payload']['payment_link']['entity']
            elif event_type in ['qr_code.credited', 'payment.captured']:
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
                        device_id="static_qr_machine" # Using a placeholder since it's a static QR
                    )
                    db.add(new_tx)
                    db.commit()
                
                # Notify ALL connected ESP32s (Assuming single-machine setup)
                logger.info(f"Broadcasting successful payment of {amount} INR to all connected devices.")
                for device_id, ws in active_connections.items():
                    try:
                        await ws.send_json({
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
                        logger.error(f"Failed to send to device {device_id}: {str(e)}")

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
            for device_id, ws in active_connections.items():
                try:
                    await ws.send_json({
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
