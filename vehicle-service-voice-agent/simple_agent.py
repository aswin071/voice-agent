"""SpeedCare Voice Agent worker.

Wires LiveKit's AgentSession (Sarvam STT + Sarvam TTS) to the project's
own ConversationalAgent state machine in `agent_core/state_machine.py`.

The state machine talks to Anthropic directly (httpx) and persists bookings
through `api/services/booking_service.py`, so we don't use a livekit LLM
plugin — instead we override `Agent.llm_node` and return the state machine's
reply text. AgentSession then routes that text into Sarvam TTS automatically.

A `_StubLLM` instance is passed to AgentSession only because the framework
short-circuits the response pipeline when `llm is None`. Its `chat()` is
never called, because our `llm_node` override fully replaces the default.

Usage:
    python simple_agent.py dev
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import AsyncIterable

import httpx
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.voice.room_io import RoomOptions
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.plugins import silero

from agent_core.state_machine import ConversationalAgent
from config import get_settings
from db import async_session
from plugins.sarvam_stt import SarvamSTT
from plugins.sarvam_tts import SarvamTTS

load_dotenv(dotenv_path=".env.local")
settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("speedcare.agent")


# Hardcoded greetings keyed by language. The first turn is spoken by the
# agent before any user input, so it doesn't go through the state machine.
GREETINGS = {
    "en": "Hello! Welcome to SpeedCare. How can I help you with your vehicle service today?",
    "ta": "வணக்கம்! ஸ்பீட்கேருக்கு வரவேற்கிறோம். உங்கள் வாகன சேவைக்கு எப்படி உதவ முடியும்?",
    "hi": "नमस्ते! स्पीडकेयर में आपका स्वागत है। मैं आपकी वाहन सेवा में कैसे मदद कर सकता हूँ?",
    "ml": "നമസ്കാരം! സ്പീഡ്‌കെയറിലേക്ക് സ്വാഗതം. നിങ്ങളുടെ വാഹന സേവനത്തിൽ എങ്ങനെ സഹായിക്കാം?",
}


class _StubLLM(llm.LLM):
    """Placeholder LLM so AgentSession runs the response pipeline.

    AgentActivity skips the entire reply path when `self.llm is None`
    (livekit-agents 1.5.x). We override `llm_node` on the Agent itself,
    so this stub's `chat()` is never invoked at runtime.
    """

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict] = NOT_GIVEN,
    ) -> llm.LLMStream:
        raise NotImplementedError(
            "_StubLLM.chat should never be called — Agent.llm_node is overridden"
        )

    @property
    def model(self) -> str:
        return "speedcare-state-machine"

    @property
    def provider(self) -> str:
        return "speedcare-internal"


class SpeedCareAgent(Agent):
    """Voice agent that delegates each turn to ConversationalAgent."""

    def __init__(self, *, language: str = "en", db=None, http_client: httpx.AsyncClient | None = None):
        super().__init__(
            instructions="SpeedCare vehicle service voice assistant.",
        )
        self._language = language
        self._db = db
        self._call_session_id = str(uuid.uuid4())

        # In-memory session state — equivalent to the dict that AgentSessionManager
        # would otherwise persist in Redis. One Agent instance per call.
        self._state: dict = {
            "call_session_id": self._call_session_id,
            "agent_state": "greeting",
            "intent": None,
            "language": language,
            "turn_count": 0,
            "collected_slots": {
                "vehicle_number": None,
                "service_type": None,
                "preferred_date": None,
                "caller_name": None,
            },
            "conversation_history": [],
        }

        self._brain = ConversationalAgent(http_client=http_client, db=db)

    async def on_enter(self) -> None:
        """Speak the opening greeting before the user has said anything."""
        greeting = GREETINGS.get(self._language, GREETINGS["en"])
        logger.info("[TTS  >> caller] %s", greeting)
        self._state["conversation_history"].append({"role": "assistant", "content": greeting})
        # `session.say` pushes the text directly through TTS, bypassing llm_node
        await self.session.say(greeting, allow_interruptions=True)

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings,
    ) -> str:
        """Replace the default LLM step with a call into the state machine.

        AgentSession invokes this after each finalized STT transcript. We pull
        the latest user message off `chat_ctx`, run one turn of the state
        machine, persist the resulting state, and return the reply text. The
        framework streams that text into the configured TTS.
        """
        transcript = self._latest_user_text(chat_ctx)
        if not transcript:
            logger.warning("[STT] empty transcript received — STT may have failed or caller was silent")
            return ""

        logger.info("[STT  << caller] %s   (state=%s)", transcript, self._state["agent_state"])

        turn_start = time.monotonic()
        try:
            result = await self._brain.process_turn(transcript, self._state)
        except Exception:
            logger.exception("state_machine_error")
            return "Sorry, I had a problem. Could you please repeat that?"
        turn_ms = int((time.monotonic() - turn_start) * 1000)

        logger.info(
            "[LLM  -> reply ] %s   (next=%s, intent=%s, tools=%s)",
            result["response_text"],
            result["next_agent_state"],
            result["intent"],
            result["tool_calls_made"],
        )
        logger.info(
            "[LATENCY      ] llm=%dms  brain_turn=%dms  (TTS happens after this)",
            result["llm_latency_ms"],
            turn_ms,
        )

        # Apply state mutations from the FSM result
        self._state["conversation_history"].append({"role": "user", "content": transcript})
        self._state["conversation_history"].append({"role": "assistant", "content": result["response_text"]})
        self._state["agent_state"] = result["next_agent_state"]
        self._state["intent"] = result["intent"]
        self._state["collected_slots"] = result["updated_slots"]
        self._state["turn_count"] += 1

        logger.info("[TTS  >> caller] %s", result["response_text"])
        return result["response_text"]

    @staticmethod
    def _latest_user_text(chat_ctx: llm.ChatContext) -> str:
        """Pull the most recent user-role message text out of a ChatContext."""
        for item in reversed(chat_ctx.items):
            if getattr(item, "role", None) != "user":
                continue
            content = getattr(item, "content", None)
            if content is None:
                continue
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, str):
                        parts.append(c)
                    else:
                        text = getattr(c, "text", None)
                        if text:
                            parts.append(text)
                if parts:
                    return " ".join(parts).strip()
        return ""


async def entrypoint(ctx: JobContext) -> None:
    logger.info("starting_agent room=%s", ctx.room.name)
    await ctx.connect()
    logger.info("connected_to_livekit")

    # Diagnostic: log every participant and track event so we can see
    # whether the user's audio is reaching the agent at all.
    @ctx.room.on("participant_connected")
    def on_participant_connected(p):
        logger.info("[ROOM] participant_connected identity=%s", p.identity)

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(p):
        logger.info("[ROOM] participant_disconnected identity=%s", p.identity)

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track, pub, participant):
        logger.info(
            "[ROOM] track_subscribed kind=%s participant=%s track_sid=%s",
            track.kind, participant.identity, track.sid,
        )

    @ctx.room.on("track_published")
    def on_track_published(pub, participant):
        logger.info(
            "[ROOM] track_published kind=%s participant=%s muted=%s",
            pub.kind, participant.identity, pub.muted,
        )

    # Log already-present participants (joined before agent)
    for p in ctx.room.remote_participants.values():
        logger.info("[ROOM] existing_participant identity=%s tracks=%d", p.identity, len(p.track_publications))

    # One DB session per call. The state machine uses it to persist bookings
    # and look up status. If the DB is unreachable the conversation still
    # flows; create_booking will simply report an error to the caller.
    db = async_session()
    # Long-lived HTTP client shared across all Anthropic calls in this call.
    # Connection reuse alone saves ~50-100ms per turn (TLS handshake + TCP).
    # 8s timeout matches the LLM call's own timeout.
    http = httpx.AsyncClient(
        timeout=8.0,
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60.0),
    )

    async def _shutdown():
        try:
            await db.close()
        except Exception:
            logger.exception("db_close_failed")
        try:
            await http.aclose()
        except Exception:
            pass

    ctx.add_shutdown_callback(_shutdown)

    # Language for this call.
    # Priority: job metadata (production/SIP) → env var (local dev) → default "en"
    # In production, dispatch sets metadata like: {"language": "ta"}
    _SUPPORTED_LANGS = ("en", "ta", "hi", "ml")
    lang = "en"
    if ctx.job and ctx.job.metadata:
        try:
            import json as _json
            _meta = _json.loads(ctx.job.metadata)
            lang = str(_meta.get("language", "en")).lower()
        except Exception:
            logger.warning("job_metadata_parse_failed, falling back to env/default")
    if lang not in _SUPPORTED_LANGS:
        lang = os.environ.get("SPEEDCARE_LANG", "en").lower()
    if lang not in _SUPPORTED_LANGS:
        logger.warning("unsupported_lang=%s, falling back to en", lang)
        lang = "en"
    logger.info("call_language=%s", lang)

    agent = SpeedCareAgent(language=lang, db=db, http_client=http)

    # Endpointing budget — every 100ms here is 100ms of perceived "agent is slow".
    # 0.25s VAD silence + 0.2s endpoint delay = ~450ms wait after caller stops.
    # Non-English speakers pause longer mid-sentence so we give them ~150ms more.
    silence = 0.25 if lang == "en" else 0.3
    session = AgentSession(
        vad=silero.VAD.load(
            min_silence_duration=silence,
            activation_threshold=0.3,   # lower = more sensitive (default 0.5)
        ),
        min_endpointing_delay=0.1,
        stt=SarvamSTT(language=lang),
        llm=_StubLLM(),
        tts=SarvamTTS(
            language=lang,
            model="bulbul:v3",
            pace=1.0,
            enable_preprocessing=True,
        ),
    )

    # noise_cancellation.BVCTelephony() is a paid LiveKit Cloud feature.
    # Removed to avoid silently breaking audio input on accounts without it.
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=RoomOptions(),
    )

    logger.info("agent_ready")


if __name__ == "__main__":
    print("╔════════════════════════════════════════════════════════╗")
    print("║  SpeedCare Voice Agent - Sarvam + Claude (FSM-driven) ║")
    print("╠════════════════════════════════════════════════════════╣")
    print("║  STT: Sarvam AI (saaras:v3)                          ║")
    print("║  LLM: Claude via ConversationalAgent state machine   ║")
    print("║  TTS: Sarvam AI (bulbul:v3)                          ║")
    print("║  DB:  PostgreSQL (booking persistence)               ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()

    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="speedcare-agent",
    ))
