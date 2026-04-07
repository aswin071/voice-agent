from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, get_request_id, mask_phone_number
from api.models import CallSession, VoiceTurn
from api.schemas import ResponseEnvelope, RoomCreateRequest, RoomEndRequest
from config import get_settings
from db import get_db

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])
settings = get_settings()


@router.post("/rooms")
async def create_room(
    body: RoomCreateRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    # Check duplicate call_sid
    existing = await db.execute(
        select(CallSession).where(CallSession.call_sid == body.call_sid)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Duplicate call_sid")

    room_name = f"SC_ROOM_{body.call_sid}"
    session = CallSession(
        call_sid=body.call_sid,
        caller_number=body.caller_number,
        agent_state="greeting",
        metadata_={
            "source": body.source,
            "sip_call_id": body.sip_call_id,
            **(body.metadata or {}),
            "request_id": request_id,
        },
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    # Generate LiveKit token for the agent
    from livekit.api import AccessToken, VideoGrants

    token = AccessToken(
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    )
    token.identity = f"agent-{session.id}"
    token.ttl = 7200
    token.video_grants = VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True)

    return ResponseEnvelope(
        data={
            "room_name": room_name,
            "livekit_token": token.to_jwt(),
            "call_session_id": str(session.id),
            "ws_url": settings.LIVEKIT_URL,
        },
        request_id=request_id,
    )


@router.post("/rooms/{room_name}/end")
async def end_room(
    room_name: str,
    body: RoomEndRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    call_sid = room_name.replace("SC_ROOM_", "")
    result = await db.execute(
        select(CallSession).where(CallSession.call_sid == call_sid)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Call session not found")

    session.ended_at = datetime.now(timezone.utc)
    session.outcome = body.outcome
    if session.started_at:
        session.duration_seconds = int(
            (session.ended_at - session.started_at).total_seconds()
        )
    await db.commit()

    return ResponseEnvelope(
        data={
            "room_name": room_name,
            "ended_at": session.ended_at.isoformat(),
            "duration_seconds": session.duration_seconds,
        },
        request_id=request_id,
    )


@router.get("/rooms/{room_name}/status")
async def room_status(
    room_name: str,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    call_sid = room_name.replace("SC_ROOM_", "")
    result = await db.execute(
        select(CallSession).where(CallSession.call_sid == call_sid)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Call session not found")

    turn_count = (
        await db.execute(
            select(func.count(VoiceTurn.id)).where(
                VoiceTurn.call_session_id == session.id
            )
        )
    ).scalar() or 0

    status = "active" if not session.ended_at else "ended"

    return ResponseEnvelope(
        data={
            "room_name": room_name,
            "call_session_id": str(session.id),
            "status": status,
            "agent_state": session.agent_state,
            "language": session.language,
            "turn_count": turn_count,
            "pipeline_health": {"stt": "ok", "tts": "ok", "llm": "ok"},
        },
        request_id=request_id,
    )


@router.get("/calls")
async def list_calls(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    outcome: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    query = select(CallSession)
    count_q = select(func.count(CallSession.id))

    if date_from:
        query = query.where(CallSession.started_at >= datetime.combine(date_from, datetime.min.time()))
        count_q = count_q.where(CallSession.started_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.where(CallSession.started_at <= datetime.combine(date_to, datetime.max.time()))
        count_q = count_q.where(CallSession.started_at <= datetime.combine(date_to, datetime.max.time()))
    if outcome:
        query = query.where(CallSession.outcome == outcome)
        count_q = count_q.where(CallSession.outcome == outcome)

    total = (await db.execute(count_q)).scalar() or 0
    results = await db.execute(
        query.order_by(CallSession.started_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        {
            "call_session_id": str(s.id),
            "call_sid": s.call_sid,
            "caller_number": mask_phone_number(s.caller_number),
            "language": s.language,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "duration_seconds": s.duration_seconds,
            "outcome": s.outcome,
            "intent": s.intent,
        }
        for s in results.scalars().all()
    ]

    return ResponseEnvelope(
        data={"total": total, "page": page, "page_size": page_size, "items": items},
        request_id=request_id,
    )
