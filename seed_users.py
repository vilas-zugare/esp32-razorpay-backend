import os
from sqlalchemy.orm import Session
from database import SessionLocal, engine
import models
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def seed():
    models.Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Schema Migrations (ignoring errors if columns already exist)
    from sqlalchemy import text
    try:
        db.execute(text("ALTER TABLE transactions ADD COLUMN payment_method VARCHAR DEFAULT 'QR'"))
        db.commit()
        print("Added payment_method column.")
    except Exception:
        db.rollback()

    try:
        db.execute(text("ALTER TABLE transactions ADD COLUMN idempotency_key VARCHAR"))
        db.commit()
        print("Added idempotency_key column.")
    except Exception:
        db.rollback()

    dummy_users = [
        {"mobile_number": "9999999991", "pin": "1111", "balance": 20.0},
        {"mobile_number": "9999999992", "pin": "2222", "balance": 20.0},
        {"mobile_number": "9999999993", "pin": "3333", "balance": 20.0},
    ]

    for d in dummy_users:
        existing = db.query(models.User).filter(models.User.mobile_number == d["mobile_number"]).first()
        if not existing:
            hashed_pin = pwd_context.hash(d["pin"])
            user = models.User(
                mobile_number=d["mobile_number"],
                pin_hash=hashed_pin,
                wallet_balance=d["balance"]
            )
            db.add(user)
            print(f"Created User -> Mobile: {d['mobile_number']} | PIN: {d['pin']} | Balance: {d['balance']}")
        else:
            print(f"User {d['mobile_number']} already exists.")

    db.commit()
    db.close()
    print("Database seeding completed.")

if __name__ == "__main__":
    seed()
