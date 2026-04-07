from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

SESSION_TTL = 4 * 3600  # 4 hours
MAX_HISTORY_TURNS = 20


class AgentSessionManager:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    def _key(self, call_session_id: str) -> str:
        return f"agent:session:{call_session_id}"

    async def create(self, call_session_id: str, language: str = "ta") -> dict:
        session = {
            "call_session_id": call_session_id,
            "agent_state": "greeting",
            "intent": None,
            "language": language,
            "turn_count": 0,
            "clarification_retries": 0,
            "out_of_scope_count": 0,
            "silence_count": 0,
            "collected_slots": {
                "vehicle_number": None,
                "service_type": None,
                "preferred_date": None,
                "caller_name": None,
            },
            "conversation_history": [],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.set(
            self._key(call_session_id),
            json.dumps(session),
            ex=SESSION_TTL,
        )
        return session

    async def get(self, call_session_id: str) -> dict | None:
        data = await self.redis.get(self._key(call_session_id))
        if data:
            return json.loads(data)
        return None

    async def update(self, call_session_id: str, session: dict) -> None:
        session["last_updated"] = datetime.now(timezone.utc).isoformat()
        # Trim history
        if len(session.get("conversation_history", [])) > MAX_HISTORY_TURNS:
            session["conversation_history"] = session["conversation_history"][-MAX_HISTORY_TURNS:]
        await self.redis.set(
            self._key(call_session_id),
            json.dumps(session),
            ex=SESSION_TTL,
        )

    async def delete(self, call_session_id: str) -> None:
        await self.redis.delete(self._key(call_session_id))

    async def add_turn(self, call_session_id: str, role: str, content: str) -> dict:
        session = await self.get(call_session_id)
        if not session:
            session = await self.create(call_session_id)
        session["conversation_history"].append({"role": role, "content": content})
        session["turn_count"] = len(session["conversation_history"])
        await self.update(call_session_id, session)
        return session
