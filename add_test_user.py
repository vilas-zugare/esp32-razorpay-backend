import os
import sys
sys.path.append('.')
from sqlalchemy.orm import Session
from database import SessionLocal, engine
import models
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def add_test_user():
    db = SessionLocal()
    mobile = "8080587261"
    pin = "9897"
    
    existing = db.query(models.User).filter(models.User.mobile_number == mobile).first()
    if not existing:
        hashed_pin = pwd_context.hash(pin)
        user = models.User(
            mobile_number=mobile,
            pin_hash=hashed_pin,
            wallet_balance=100.0  # Give some test balance
        )
        db.add(user)
        db.commit()
        print(f"Successfully created User -> Mobile: {mobile} | PIN: {pin} | Balance: 100.0")
    else:
        existing.pin_hash = pwd_context.hash(pin)
        db.commit()
        print(f"User {mobile} already existed, updated PIN to {pin}.")
        
    db.close()

if __name__ == "__main__":
    add_test_user()
