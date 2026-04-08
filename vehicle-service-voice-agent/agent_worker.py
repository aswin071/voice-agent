"""SpeedCare Voice Agent Worker.

This is the LiveKit agent worker that handles voice conversations.
It connects to LiveKit rooms and processes audio through:
    VAD → Sarvam STT → Claude LLM → Sarvam TTS

Usage:
    python agent_worker.py dev

Environment:
    Requires LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET in .env.local
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from livekit import rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    JobContext,
    JobRequest,
    RoomInputOptions,
    WorkerOptions,
    cli,
    get_job_context,
    function_tool,
    RunContext,
)
from livekit.plugins import silero, noise_cancellation

from agent_core.session import AgentSessionManager
from agent_core.state_machine import ConversationalAgent
from agent_core.prompts import GREETINGS, FALLBACK_MESSAGES
from api.services.booking_service import create_booking
from plugins.sarvam_stt import SarvamSTT
from plugins.sarvam_tts import SarvamTTS
from config import get_settings
import redis.asyncio as aioredis

load_dotenv(dotenv_path=".env.local")
settings = get_settings()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("speedcare.agent_worker")


class SpeedCareVoiceAgent(Agent):
    """SpeedCare Voice Agent - Handles vehicle service booking calls."""

    def __init__(self):
        super().__init__(
            instructions="""You are SpeedCare's voice assistant for vehicle service bookings.
You help customers book vehicle service appointments in Tamil, Hindi, English, or Malayalam.
Be polite, professional, and efficient. Collect: vehicle number, service type, preferred date, caller name."""
        )
        self.participant: rtc.RemoteParticipant | None = None
        self.call_session_id: str | None = None
        self.language: str = "ta"  # Default to Tamil
        self.session_manager: AgentSessionManager | None = None
        self.agent_core: ConversationalAgent | None = None
        self.redis: aioredis.Redis | None = None
        self.db = None  # Will be set up per-job if needed
        self.turn_count = 0

    async def connect_redis(self):
        """Connect to Redis for session management."""
        self.redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
        self.session_manager = AgentSessionManager(self.redis)

    async def start_session(self, call_sid: str, caller_number: str | None = None):
        """Initialize a new call session."""
        self.call_session_id = str(uuid.uuid4())

        if not self.session_manager:
            await self.connect_redis()

        # Create session in Redis
        session = await self.session_manager.create(
            self.call_session_id,
            language=self.language,
        )

        # Register with FastAPI backend
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://localhost:8000/api/v1/voice/rooms",
                    json={
                        "call_sid": call_sid or self.call_session_id[:8],
                        "caller_number": caller_number,
                        "source": "webrtc",
                    },
                    headers={"X-Api-Key": "test-key"},
                )
        except Exception as e:
            logger.warning(f"Could not register with backend: {e}")

        return session

    async def on_enter(self):
        """Called when the agent joins the room."""
        logger.info(f"Agent entered room: {self.call_session_id}")

    async def on_exit(self):
        """Called when the agent leaves the room."""
        logger.info(f"Agent exited room: {self.call_session_id}")
        if self.redis:
            await self.redis.close()

    @function_tool()
    async def identify_intent(self, ctx: RunContext, intent: str, confidence: float):
        """Identify caller's intent from their message.

        Args:
            intent: One of booking_new, booking_status, service_inquiry, out_of_scope
            confidence: Confidence score 0.0-1.0
        """
        logger.info(f"Intent identified: {intent} (confidence: {confidence})")
        return {"intent": intent, "confidence": confidence}

    @function_tool()
    async def collect_slot(self, ctx: RunContext, slot_name: str, slot_value: str):
        """Collect a booking slot from the caller.

        Args:
            slot_name: Name of the slot (vehicle_number, service_type, preferred_date, caller_name)
            slot_value: Value provided by the caller
        """
        logger.info(f"Slot collected: {slot_name} = {slot_value}")
        return {"slot": slot_name, "value": slot_value}

    @function_tool()
    async def end_call(self, ctx: RunContext, outcome: str = "completed"):
        """End the call gracefully.

        Args:
            outcome: Call outcome - completed, failed, abandoned, transferred
        """
        logger.info(f"Ending call with outcome: {outcome}")
        job_ctx = get_job_context()

        # End the room
        try:
            await job_ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=job_ctx.room.name)
            )
        except Exception as e:
            logger.error(f"Error ending call: {e}")

        return {"status": "ended", "outcome": outcome}

    async def on_user_message(self, message: str, ctx: RunContext):
        """Process user message through the agent state machine."""
        self.turn_count += 1
        logger.info(f"Turn {self.turn_count}: User said: {message}")

        # Get session from Redis
        if not self.session_manager:
            await self.connect_redis()

        session = await self.session_manager.get(self.call_session_id)
        if not session:
            session = await self.start_session(self.call_session_id)

        # Add user message to history
        await self.session_manager.add_turn(
            self.call_session_id, "user", message
        )

        # Create agent core with HTTP client for LLM
        agent_core = ConversationalAgent()

        # Process through state machine
        result = await agent_core.process_turn(message, session)

        # Update session
        session["agent_state"] = result["next_agent_state"]
        session["intent"] = result.get("intent", session.get("intent"))
        session["collected_slots"] = result["updated_slots"]
        await self.session_manager.update(self.call_session_id, session)

        # Add agent response to history
        await self.session_manager.add_turn(
            self.call_session_id, "assistant", result["response_text"]
        )

        logger.info(f"Agent response: {result['response_text'][:100]}...")

        # Speak the response
        await ctx.session.generate_reply(
            instructions=result["response_text"]
        )

        # If booking confirmed, create it
        if result.get("action") == "booking_confirmed":
            logger.info("Booking confirmed, creating in database...")
            # Note: Would need DB session here for full implementation


async def entrypoint(ctx: JobContext):
    """Entry point for LiveKit agent worker."""
    logger.info(f"Connecting to room: {ctx.room.name}")
    await ctx.connect()

    # Create the agent
    agent = SpeedCareVoiceAgent()

    # Initialize session
    await agent.start_session(ctx.room.name)

    # Set up the audio pipeline
    # Note: In a full implementation, we'd integrate:
    # - SarvamSTT for speech-to-text
    # - SarvamTTS for text-to-speech
    # - Claude via state_machine for responses

    # For now, use simpler pipeline with native LiveKit plugins
    from livekit.plugins import openai as livekit_openai

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=livekit_openai.STT(),  # Temporary: using OpenAI whisper
        llm=livekit_openai.LLM(model="gpt-4o"),  # Temporary: using OpenAI
        tts=livekit_openai.TTS(),  # Temporary: using OpenAI
    )

    # Start agent
    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    # Create room and join (for WebRTC testing)
    # In production, this would be triggered by an incoming call

    logger.info("Agent session started, waiting for participants...")

    # Wait for participant
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"Participant joined: {participant.identity}")
        agent.participant = participant

        # Play greeting
        greeting = GREETINGS.get(agent.language, GREETINGS["en"])
        asyncio.create_task(
            session.generate_reply(instructions=greeting)
        )


async def request_fnc(req: JobRequest) -> None:
    """Accept all job requests."""
    logger.info(f"Accepting job for room: {req.room.name}")
    await req.accept(entrypoint)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="speedcare-agent-worker",
    ))
