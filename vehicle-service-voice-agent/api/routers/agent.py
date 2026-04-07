"""Agent API Router - Conversational agent endpoints.

Provides endpoints for processing conversation turns, retrieving session context,
and operator overrides for the voice agent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.session import AgentSessionManager
from agent_core.state_machine import ConversationalAgent
from api.deps import get_db, get_redis, get_request_id, verify_api_key
from api.models import AgentTurn, CallSession
from api.schemas import ProcessTurnRequest, ProcessTurnResponse, ResponseEnvelope

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


@router.post("/process-turn")
async def process_turn(
    body: ProcessTurnRequest,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    request_id: str = Depends(get_request_id),
) -> ResponseEnvelope:
    """Process a single conversation turn.

    Called internally by the agent worker after receiving a final STT transcript.
    Processes the turn through the agent state machine and returns the response
    along with updated state and slot information.
    """
    # Get or create session
    session_manager = AgentSessionManager(redis)
    session = await session_manager.get(str(body.call_session_id))

    if not session:
        # Try to load from DB or create new
        result = await db.execute(
            select(CallSession).where(CallSession.id == body.call_session_id)
        )
        call_session = result.scalar_one_or_none()

        if call_session:
            session = await session_manager.create(
                str(body.call_session_id),
                language=call_session.language or body.language,
            )
            # Restore state from DB
            session["agent_state"] = call_session.agent_state or "greeting"
            session["intent"] = call_session.intent
        else:
            session = await session_manager.create(
                str(body.call_session_id),
                language=body.language,
            )

    # Update language from request if not set
    if body.language and not session.get("language"):
        session["language"] = body.language

    # Update collected slots from request
    if body.collected_slots:
        session["collected_slots"].update(body.collected_slots)

    # Add user transcript to history
    await session_manager.add_turn(
        str(body.call_session_id),
        "user",
        body.transcript,
    )

    # Process through state machine with DB access
    agent = ConversationalAgent(db=db)
    result = await agent.process_turn(body.transcript, session)

    # Update session with results
    session["agent_state"] = result["next_agent_state"]
    session["intent"] = result.get("intent", session.get("intent"))
    session["collected_slots"] = result["updated_slots"]

    # Add agent response to history
    await session_manager.add_turn(
        str(body.call_session_id),
        "assistant",
        result["response_text"],
    )

    # Persist turn to database
    agent_turn = AgentTurn(
        call_session_id=body.call_session_id,
        turn_number=body.turn_number,
        transcript=body.transcript,
        intent_classified=result.get("intent"),
        agent_response=result["response_text"],
        agent_state_before=body.agent_state,
        agent_state_after=result["next_agent_state"],
        slots_before=body.collected_slots,
        slots_after=result["updated_slots"],
        tool_calls={"calls": result.get("tool_calls_made", [])},
        llm_model="claude-haiku-4-5",
        llm_latency_ms=result.get("llm_latency_ms", 0),
    )
    db.add(agent_turn)

    # Update call session in DB
    result_db = await db.execute(
        select(CallSession).where(CallSession.id == body.call_session_id)
    )
    call_session = result_db.scalar_one_or_none()
    if call_session:
        call_session.agent_state = result["next_agent_state"]
        call_session.intent = result.get("intent", call_session.intent)
        call_session.language = session.get("language", call_session.language)

    await db.commit()

    return ResponseEnvelope(
        data={
            "response_text": result["response_text"],
            "next_agent_state": result["next_agent_state"],
            "intent": result.get("intent"),
            "updated_slots": result["updated_slots"],
            "tool_calls_made": result.get("tool_calls_made", []),
            "slots_remaining": result.get("slots_remaining", []),
            "action": result.get("action", "respond"),
            "llm_latency_ms": result.get("llm_latency_ms", 0),
        },
        request_id=request_id,
    )


@router.get("/sessions/{call_session_id}/context")
async def get_session_context(
    call_session_id: uuid.UUID,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    request_id: str = Depends(get_request_id),
) -> ResponseEnvelope:
    """Retrieve the full conversation context for a call.

    Used by dashboard and debugging to view the complete conversation history,
    collected slots, and current agent state.
    """
    # Try Redis first
    session_manager = AgentSessionManager(redis)
    session = await session_manager.get(str(call_session_id))

    if session:
        # Get turn counts from DB
        turn_count_result = await db.execute(
            select(func.count(AgentTurn.id)).where(
                AgentTurn.call_session_id == call_session_id
            )
        )
        turn_count = turn_count_result.scalar() or 0

        return ResponseEnvelope(
            data={
                "call_session_id": str(call_session_id),
                "agent_state": session.get("agent_state", "greeting"),
                "intent": session.get("intent"),
                "language": session.get("language", "ta"),
                "turn_count": turn_count,
                "collected_slots": session.get("collected_slots", {}),
                "conversation_history": session.get("conversation_history", []),
                "retry_counts": {
                    "clarification": session.get("clarification_retries", 0),
                    "llm_retry": 0,  # Tracked in session if needed
                },
                "source": "redis",
            },
            request_id=request_id,
        )

    # Fallback to DB
    result = await db.execute(
        select(CallSession).where(CallSession.id == call_session_id)
    )
    call_session = result.scalar_one_or_none()

    if not call_session:
        raise HTTPException(status_code=404, detail="Call session not found")

    # Get turns from DB
    turns_result = await db.execute(
        select(AgentTurn)
        .where(AgentTurn.call_session_id == call_session_id)
        .order_by(AgentTurn.turn_number)
    )
    turns = turns_result.scalars().all()

    # Reconstruct conversation history
    history = []
    for turn in turns:
        history.append({"role": "user", "content": turn.transcript})
        history.append({"role": "assistant", "content": turn.agent_response})

    return ResponseEnvelope(
        data={
            "call_session_id": str(call_session_id),
            "agent_state": call_session.agent_state or "greeting",
            "intent": call_session.intent,
            "language": call_session.language or "ta",
            "turn_count": len(turns),
            "collected_slots": turns[-1].slots_after if turns else {},
            "conversation_history": history,
            "retry_counts": {"clarification": 0, "llm_retry": 0},
            "source": "database",
        },
        request_id=request_id,
    )


@router.post("/sessions/{call_session_id}/override")
async def override_session_state(
    call_session_id: uuid.UUID,
    new_state: str,
    reason: str = "operator_intervention",
    force_slots: dict | None = None,
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    request_id: str = Depends(get_request_id),
) -> ResponseEnvelope:
    """Operator override: manually advance or reset agent state.

    Used by dashboard supervisors to intervene in stuck or problematic calls.
    Allows forcing state transitions and slot values.
    """
    # Get Redis session
    session_manager = AgentSessionManager(redis)
    session = await session_manager.get(str(call_session_id))

    result = await db.execute(
        select(CallSession).where(CallSession.id == call_session_id)
    )
    call_session = result.scalar_one_or_none()

    if not call_session and not session:
        raise HTTPException(status_code=404, detail="Call session not found")

    previous_state = None

    if session:
        previous_state = session.get("agent_state")
        session["agent_state"] = new_state
        if force_slots:
            session["collected_slots"].update(force_slots)
        await session_manager.update(str(call_session_id), session)

    if call_session:
        previous_state = call_session.agent_state or previous_state
        call_session.agent_state = new_state
        call_session.updated_at = datetime.now(timezone.utc)
        await db.commit()

    # Log the override
    from api.models import AuditLog
    audit = AuditLog(
        entity_type="call_session",
        entity_id=call_session_id,
        action="AGENT_OVERRIDE",
        actor_type="operator",
        diff={
            "previous_state": previous_state,
            "new_state": new_state,
            "reason": reason,
            "force_slots": force_slots,
        },
        request_id=uuid.UUID(request_id) if request_id else None,
    )
    db.add(audit)
    await db.commit()

    return ResponseEnvelope(
        data={
            "previous_state": previous_state,
            "new_state": new_state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        },
        request_id=request_id,
    )


@router.get("/turns")
async def list_agent_turns(
    call_session_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    api_key=Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
) -> ResponseEnvelope:
    """List agent turns with filtering.

    Used for debugging and analytics to review conversation turns,
    LLM latencies, and intent classifications.
    """
    query = select(AgentTurn)
    count_q = select(func.count(AgentTurn.id))

    if call_session_id:
        query = query.where(AgentTurn.call_session_id == call_session_id)
        count_q = count_q.where(AgentTurn.call_session_id == call_session_id)

    total = (await db.execute(count_q)).scalar() or 0

    results = await db.execute(
        query.order_by(AgentTurn.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    items = [
        {
            "id": t.id,
            "call_session_id": str(t.call_session_id),
            "turn_number": t.turn_number,
            "transcript": t.transcript,
            "intent_classified": t.intent_classified,
            "agent_response": t.agent_response,
            "agent_state_before": t.agent_state_before,
            "agent_state_after": t.agent_state_after,
            "llm_latency_ms": t.llm_latency_ms,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in results.scalars().all()
    ]

    return ResponseEnvelope(
        data={
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        },
        request_id=request_id,
    )
