from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import (
    create_access_token, get_current_user, get_db, get_redis, get_request_id,
)
from api.models import User
from api.schemas import (
    ApiKeyCreateRequest, LiveKitTokenRequest, LoginRequest, LogoutRequest,
    RefreshRequest, ResponseEnvelope, TokenResponse,
)
from api.services.auth_service import (
    authenticate_user, create_refresh_token_record, generate_api_key,
    revoke_refresh_token, validate_refresh_token, write_audit_log,
)
from config import get_settings
from db import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
settings = get_settings()
logger = logging.getLogger("speedcare.api.auth")

# Worker registers with this name in simple_agent.py / agent_worker.py.
# Because the worker uses an explicit agent_name, LiveKit will NOT auto-dispatch
# it to rooms — we have to create an AgentDispatch ourselves whenever we hand
# out a participant token, otherwise the caller joins a room with no agent in it.
LIVEKIT_AGENT_NAME = "speedcare-agent"


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    # Rate limiting check
    redis = await get_redis()
    ip = request.client.host if request.client else "unknown"
    rate_key = f"ratelimit:login:{ip}"
    attempts = await redis.incr(rate_key)
    if attempts == 1:
        await redis.expire(rate_key, 900)
    if attempts > 5:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    user, error = await authenticate_user(db, body.email, body.password, ip)
    if error:
        if error == "ACCOUNT_LOCKED":
            raise HTTPException(status_code=429, detail="Account locked. Try again in 15 minutes.")
        if error == "ACCOUNT_DISABLED":
            raise HTTPException(status_code=403, detail="Account disabled.")
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    access_token = create_access_token(str(user.id), user.role)
    refresh_token = await create_refresh_token_record(db, user.id, ip)

    await write_audit_log(db, "user", user.id, "LOGIN", actor_id=user.id, ip=ip)

    return ResponseEnvelope(
        data=TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user={
                "id": str(user.id),
                "email": user.email,
                "role": user.role,
                "name": user.name,
            },
        ).model_dump(),
        request_id=request_id,
    )


@router.post("/refresh")
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    record = await validate_refresh_token(db, body.refresh_token)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    # Revoke old, issue new (rotation)
    await revoke_refresh_token(db, body.refresh_token)

    result = await db.execute(select(User).where(User.id == record.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found.")

    new_access = create_access_token(str(user.id), user.role)
    new_refresh = await create_refresh_token_record(db, user.id)

    return ResponseEnvelope(
        data={"access_token": new_access, "refresh_token": new_refresh, "expires_in": 900},
        request_id=request_id,
    )


@router.post("/logout")
async def logout(
    body: LogoutRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    await revoke_refresh_token(db, body.refresh_token)
    await write_audit_log(db, "user", user.id, "LOGOUT", actor_id=user.id)
    return ResponseEnvelope(data={"message": "Logged out successfully."}, request_id=request_id)


@router.post("/livekit-token")
async def livekit_token(
    body: LiveKitTokenRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    import jwt as pyjwt
    from datetime import datetime, timezone

    # Build claims manually for proper LiveKit token
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=min(body.ttl_seconds, 7200))

    claims = {
        "sub": body.participant_identity,
        "iss": settings.LIVEKIT_API_KEY,
        "nbf": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "video": {
            "roomJoin": True,
            "room": body.call_sid,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }

    token = pyjwt.encode(
        claims,
        settings.LIVEKIT_API_SECRET,
        algorithm="HS256",
    )

    # Create an AgentDispatch so the named worker actually joins this room.
    # Without this the browser connects to an empty room and hears nothing —
    # see plans/binary-swimming-lighthouse.md for the full root-cause writeup.
    from livekit import api as lkapi

    lk = lkapi.LiveKitAPI(
        url=settings.LIVEKIT_URL,
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    )
    try:
        await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name=LIVEKIT_AGENT_NAME,
                room=body.call_sid,
                metadata="",
            )
        )
        logger.info(
            "livekit_dispatch_created",
            extra={"room": body.call_sid, "agent": LIVEKIT_AGENT_NAME},
        )
    except Exception as e:
        # If a dispatch already exists for this (agent, room), LiveKit returns
        # AlreadyExists — that's fine, the worker is already on its way.
        msg = str(e).lower()
        if "already" in msg or "exists" in msg:
            logger.info(
                "livekit_dispatch_already_exists",
                extra={"room": body.call_sid, "agent": LIVEKIT_AGENT_NAME},
            )
        else:
            logger.exception(
                "livekit_dispatch_failed",
                extra={"room": body.call_sid, "agent": LIVEKIT_AGENT_NAME},
            )
            raise HTTPException(
                status_code=502,
                detail=f"agent dispatch failed: {e}",
            )
    finally:
        await lk.aclose()

    return ResponseEnvelope(
        data={
            "token": token,
            "room_name": body.call_sid,
        },
        request_id=request_id,
    )

@router.post("/livekit-token-debug")
async def livekit_token_debug(
    body: LiveKitTokenRequest,
    request_id: str = Depends(get_request_id),
):
    """Debug endpoint to check LiveKit configuration without generating token."""
    import jwt

    # Check if we can decode existing tokens (not generate new ones)
    return ResponseEnvelope(
        data={
            "livekit_url": settings.LIVEKIT_URL,
            "api_key_first_4": settings.LIVEKIT_API_KEY[:4] if settings.LIVEKIT_API_KEY else None,
            "api_secret_length": len(settings.LIVEKIT_API_SECRET) if settings.LIVEKIT_API_SECRET else 0,
            "room": body.call_sid,
            "identity": body.participant_identity,
        },
        request_id=request_id,
    )


@router.post("/api-keys")
async def create_api_key(
    body: ApiKeyCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    from api.models import ApiKey

    raw_key, prefix, hashed = generate_api_key()
    api_key = ApiKey(
        name=body.name,
        key_prefix=prefix,
        hashed_key=hashed,
        role=body.role,
        description=body.description,
        expires_at=body.expires_at,
        created_by=user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    await write_audit_log(db, "api_key", api_key.id, "CREATE", actor_id=user.id)

    return ResponseEnvelope(
        success=True,
        data={
            "id": str(api_key.id),
            "name": api_key.name,
            "key": raw_key,
            "prefix": prefix,
            "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
        },
        request_id=request_id,
    )


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    from api.models import ApiKey

    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    key.is_active = False
    await db.commit()
    await write_audit_log(db, "api_key", key.id, "REVOKE", actor_id=user.id)

    return ResponseEnvelope(data={"revoked": True, "key_id": str(key_id)}, request_id=request_id)
