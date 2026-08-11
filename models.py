from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from database import Base
import datetime

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, index=True, nullable=True) # E.g., if you use orders
    payment_link_id = Column(String, index=True, nullable=True)
    payment_id = Column(String, index=True, nullable=True) # Razorpay payment ID
    amount = Column(Float, nullable=False) # In INR (not paise)
    status = Column(String, default="created") # created, paid, cancelled, failed
    method = Column(String, nullable=True) # upi, card, etc.
    device_id = Column(String, index=True, nullable=True)
    payment_method = Column(String, default="QR")
    idempotency_key = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    mobile_number = Column(String, unique=True, index=True, nullable=False)
    pin_hash = Column(String, nullable=False)
    wallet_balance = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class RewardCode(Base):
    __tablename__ = "reward_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    value = Column(Float, default=1.50)
    status = Column(String, default="UNCLAIMED") # UNCLAIMED, CLAIMED
    claimed_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class RateLimit(Base):
    __tablename__ = "rate_limits"

    id = Column(Integer, primary_key=True, index=True)
    mobile_number = Column(String, index=True, nullable=False)
    failed_attempts = Column(Integer, default=0)
    last_attempt = Column(DateTime, default=datetime.datetime.utcnow)

class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, unique=True, index=True) # The ESP32 MAC or unique ID
    ip_address = Column(String, nullable=True)
    status = Column(String, default="offline") # online, offline
    last_ping = Column(DateTime, default=datetime.datetime.utcnow)
    current_action = Column(String, default="Idle")

class WebhookLog(Base):
    __tablename__ = "webhook_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, index=True)
    payload = Column(String) # Raw JSON string
    status = Column(String) # Valid, Invalid
    received_at = Column(DateTime, default=datetime.datetime.utcnow)

class CoinsTransaction(Base):
    __tablename__ = "coins_transaction"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    transaction_type = Column(String, nullable=False) # 'add_money', 'juice_code', 'use_coins'
    amount = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
