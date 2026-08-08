import os
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv

load_dotenv()

# We can store ADMIN_USER and ADMIN_PASS in .env, with defaults if not present
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
# Default password 'admin' hashed with bcrypt for demonstration. In production, provide a strong hash.
# hash of 'admin'
DEFAULT_ADMIN_HASH = "$2b$12$48aaJB3WCZs53Lcfo0C9w.w0IPPZQ3qygzSPWVMdJeRqAk6xLG9Aa" 
ADMIN_PASS_HASH = os.getenv("ADMIN_PASS_HASH", DEFAULT_ADMIN_HASH)

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-for-jwt-super-secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 day

import bcrypt
from fastapi import Request, HTTPException, status

# The OAuth2PasswordBearer expects a token in the Authorization header.
# For a standard HTML Dashboard, we often use cookies instead. 
# We'll create a custom dependency to extract the token from a cookie.
def get_token_from_cookie(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    # The token usually looks like "Bearer <token>"
    if token.startswith("Bearer "):
        token = token.split(" ")[1]
    return token

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # Ensure both are bytes for bcrypt
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    try:
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except ValueError:
        return False

def get_password_hash(password: str) -> str:
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_admin(token: str = Depends(get_token_from_cookie)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None or username != ADMIN_USER:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    return username
