from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import ApiKey, User
from config import get_settings
from db import get_db

settings = get_settings()

# Redis connection pool (singleton)
_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_POOL_MAX,
            decode_responses=True,
        )
    return _redis_pool


def create_access_token(user_id: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "role": role,
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(
    request: Request,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    token = authorization.split(" ", 1)[1]
    payload = decode_access_token(token)

    # Check blocklist
    redis = await get_redis()
    jti = payload.get("jti")
    if jti and await redis.exists(f"blocklist:jti:{jti}"):
        raise HTTPException(status_code=401, detail="Token revoked")

    result = await db.execute(select(User).where(User.id == payload["sub"]))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def require_role(*roles: str):
    async def checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return checker


async def verify_api_key(
    x_api_key: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> ApiKey | None:
    """Verify X-Api-Key header. Returns ApiKey or raises 401."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    prefix = x_api_key[:12]
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.is_active == True)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    import bcrypt
    if not bcrypt.checkpw(x_api_key.encode(), api_key.hashed_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")

    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="API key expired")

    # Update last_used_at (fire-and-forget)
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    return api_key


def get_request_id(x_request_id: str | None = Header(None)) -> str:
    return x_request_id or str(uuid.uuid4())


def mask_phone_number(number: str | None) -> str:
    if not number or len(number) < 6:
        return number or ""
    return number[:3] + " XXXXX X" + number[-4:]
