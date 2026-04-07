from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import ApiKey, AuditLog, RefreshToken, User
from config import get_settings

settings = get_settings()

PASSWORD_POLICY_MIN_LENGTH = 12


def validate_password(password: str) -> str | None:
    if len(password) < PASSWORD_POLICY_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_POLICY_MIN_LENGTH} characters"
    if not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit"
    if not any(c in "!@#$%^&*()-_=+[]{}|;:',.<>?/`~" for c in password):
        return "Password must contain at least one special character"
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def generate_refresh_token() -> str:
    return os.urandom(64).hex()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def authenticate_user(db: AsyncSession, email: str, password: str, ip: str | None = None) -> tuple[User | None, str | None]:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        # Constant-time: still do a bcrypt check to prevent timing attack
        bcrypt.checkpw(b"dummy", bcrypt.gensalt(rounds=12))
        return None, "INVALID_CREDENTIALS"

    if not user.is_active:
        return None, "ACCOUNT_DISABLED"

    if user.locked_until and user.locked_until > datetime.now(timezone.utc):
        return None, "ACCOUNT_LOCKED"

    if not verify_password(password, user.hashed_password):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= 5:
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        await db.commit()
        return None, "INVALID_CREDENTIALS"

    # Success
    user.failed_attempts = 0
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return user, None


async def create_refresh_token_record(db: AsyncSession, user_id: uuid.UUID, ip: str | None = None) -> str:
    raw_token = generate_refresh_token()
    record = RefreshToken(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        ip_address=ip,
    )
    db.add(record)
    await db.commit()
    return raw_token


async def validate_refresh_token(db: AsyncSession, raw_token: str) -> RefreshToken | None:
    token_hash = hash_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(timezone.utc),
        )
    )
    return result.scalar_one_or_none()


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> bool:
    token_hash = hash_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()
    if record:
        record.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        return True
    return False


async def write_audit_log(
    db: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    diff: dict | None = None,
    ip: str | None = None,
):
    log = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        request_id=request_id,
        diff=diff,
        ip_address=ip,
    )
    db.add(log)
    await db.commit()


def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, hashed_key)."""
    raw = "sc_live_" + os.urandom(32).hex()
    prefix = raw[:12]
    hashed = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=12)).decode()
    return raw, prefix, hashed
