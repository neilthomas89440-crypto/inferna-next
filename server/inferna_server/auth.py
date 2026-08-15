"""Auth: bcrypt hashing, JWT tokens, FastAPI auth dependencies."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from inferna_server.config import get_settings
from inferna_server.db import get_db
from inferna_server.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
TOKEN_TTL = timedelta(hours=24)

# Synthetic admin used when INFERNA_AUTH_ENABLED=false (dev escape hatch).
SYNTHETIC_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_token(user: User) -> str:
    payload = {"sub": str(user.id), "exp": datetime.now(timezone.utc) + TOKEN_TTL}
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not get_settings().auth_enabled:
        return User(
            id=SYNTHETIC_ADMIN_ID,
            username="admin",
            role="admin",
            is_active=True,
            password_hash="",
        )
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise _unauthorized() from None
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return user
