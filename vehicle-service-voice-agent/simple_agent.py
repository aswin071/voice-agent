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
from livekit.agents.voice.turn import TurnHandlingOptions
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.plugins import silero

from agent_core.state_machine import ConversationalAgent
from config import get_settings
from db import async_session
from plugins.sarvam_pronunciation import get_or_create_speedcare_dict
from plugins.sarvam_stt import SarvamSTT
from plugins.sarvam_tts import SarvamTTS

load_dotenv(dotenv_path=".env.local", override=True)
settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("speedcare.agent")

# ---------------------------------------------------------------------------
# Per-language TTS voice profiles
# Each entry defines the optimal SarvamTTS constructor kwargs for that language.
# "voice" is from the bulbul:v3 catalogue; "pace" is tuned for natural cadence.
# These are the best-practice defaults — operators can override via job metadata.
# ---------------------------------------------------------------------------
_SUPPORTED_LANGS = ("en", "ta", "hi", "ml")

VOICE_PROFILES: dict[str, dict] = {
    "en": {
        "voice": "ishita",    # Clear, warm, neutral Indian-English accent
        "pace": 1.0,
        "speech_sample_rate": 24_000,
        "output_codec": "wav",
        "temperature": 0.5,   # Professional, IVR-clear (0.5 = less expressive)
    },
    "ta": {
        "voice": "ishita",    # Excels across all Indian languages (Sarvam docs)
        "pace": 1.0,
        "speech_sample_rate": 24_000,
        "output_codec": "wav",
        "temperature": 0.6,   # Warm natural default for Indian languages
    },
    "hi": {
        "voice": "ishita",    # Warm, natural Hindustani cadence
        "pace": 1.0,
        "speech_sample_rate": 24_000,
        "output_codec": "wav",
        "temperature": 0.6,
    },
    "ml": {
        "voice": "ishita",    # Handles Malayalam phonology naturally
        "pace": 1.0,
        "speech_sample_rate": 24_000,
        "output_codec": "wav",
        "temperature": 0.6,
    },
}

# Module-level pronunciation dict_id — resolved once at first entrypoint call,
# reused by all subsequent concurrent calls. None = not yet resolved.
_CACHED_DICT_ID: str | None = None
_DICT_RESOLVED: bool = False


def _build_async_client(*, timeout: float, limits: httpx.Limits) -> httpx.AsyncClient:
    """Prefer HTTP/2, but don't crash the worker if `h2` isn't installed."""
    try:
        return httpx.AsyncClient(
            timeout=timeout,
            http2=True,
            limits=limits,
        )
    except ImportError:
        logger.warning("http2_not_available_falling_back_to_http1")
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
        )


# Hardcoded greetings keyed by language. The first turn is spoken by the
# agent before any user input, so it doesn't go through the state machine.
GREETINGS = {
    "en": "Hello! Welcome to SpeedCare. How can I help you with your vehicle service today?",
    "ta": "வணக்கம்! ஸ்பீட்கேருக்கு வரவேற்கிறோம். உங்கள் வாகன சேவைக்கு எப்படி உதவ முடியும்?",
    "hi": "नमस्ते! स्पीडकेयर में आपका स्वागत है। मैं आपकी वाहन सेवा में कैसे मदद कर सकता हूँ?",
    "ml": "നമസ്കാരം! സ്പീഡ്‌കെയറിലേക്ക് സ്വാഗതം. നിങ്ങളുടെ വാഹനസർവീസിനായി എന്ത് help ആണ് വേണ്ടത്?",
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

    def __init__(self, *, language: str = "en", voice: str | None = None, db=None, http_client: httpx.AsyncClient | None = None):
        super().__init__(
            instructions="SpeedCare vehicle service voice assistant.",
        )
        self._language = language
        self._voice = voice  # explicit override from dispatch metadata
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
        await self.session.say(greeting, allow_interruptions=False)

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings,
    ) -> AsyncIterable[str]:
        """Replace the default LLM step with the streaming state machine path.

        `process_turn_stream` yields text chunks as they arrive from Claude
        (SSE). The LiveKit framework batches chunks at sentence boundaries and
        dispatches each completed sentence to Sarvam TTS immediately — so TTS
        synthesis of sentence 1 overlaps with LLM generation of sentence 2.
        Time-to-first-audio drops by roughly half the total LLM generation time.

        Session state is mutated in-place by `process_turn_stream` once the
        generator is exhausted — agent_state, collected_slots, intent, history.
        """
        transcript = self._latest_user_text(chat_ctx)
        if not transcript:
            logger.warning("[STT] empty transcript received — STT may have failed or caller was silent")
            return

        logger.info("[STT  << caller] %s   (state=%s)", transcript, self._state["agent_state"])

        turn_start = time.monotonic()
        full_text: list[str] = []

        try:
            async for chunk in self._brain.process_turn_stream(transcript, self._state):
                full_text.append(chunk)
                yield chunk
        except Exception:
            logger.exception("state_machine_stream_error")
            yield "Sorry, I had a problem. Could you please repeat that?"
            return

        turn_ms = int((time.monotonic() - turn_start) * 1000)
        reply = "".join(full_text)

        logger.info(
            "[LLM  -> reply ] %s   (next=%s, intent=%s)",
            reply,
            self._state["agent_state"],
            self._state.get("intent"),
        )
        logger.info("[LATENCY      ] brain_turn=%dms  (TTS overlapped with generation)", turn_ms)
        logger.info("[TTS  >> caller] %s", reply)

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
    http = _build_async_client(
        timeout=settings.LLM_HTTP_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60.0),
    )
    # Shared Sarvam transport reduces per-turn connection churn because STT and
    # TTS hit the same host in sequence on most turns.
    sarvam_http = _build_async_client(
        timeout=30.0,
        limits=httpx.Limits(
            max_connections=8,
            max_keepalive_connections=4,
            keepalive_expiry=90.0,
        ),
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
        try:
            await sarvam_http.aclose()
        except Exception:
            pass

    ctx.add_shutdown_callback(_shutdown)

    # ── Pronunciation dictionary (resolved once per worker process) ────────
    # get_or_create_speedcare_dict() caches the dict_id internally after the
    # first call, so concurrent entrypoints all get the same id immediately.
    global _CACHED_DICT_ID, _DICT_RESOLVED
    if not _DICT_RESOLVED:
        _CACHED_DICT_ID = await get_or_create_speedcare_dict()
        _DICT_RESOLVED = True

    # ── Language & voice resolution ────────────────────────────────────────
    # Priority order:
    #   1. job.metadata JSON  → production / SIP dispatch path
    #   2. env var            → local dev / CI
    #   3. config default
    #
    # Full metadata format:
    #   {"language":"ta", "voice":"ishita", "temperature":0.7,
    #    "pace":1.0, "stt_mode":"codemix"}
    # All keys are optional — missing keys fall through to config/profile defaults.
    lang: str | None = None
    voice_override: str | None = None
    temperature_override: float | None = None
    pace_override: float | None = None
    stt_mode_override: str | None = None

    if ctx.job and ctx.job.metadata:
        try:
            import json as _json
            _meta = _json.loads(ctx.job.metadata)
            lang = str(_meta.get("language", "")).lower() or None
            voice_override = str(_meta.get("voice", "")).lower() or None
            stt_mode_override = str(_meta.get("stt_mode", "")).lower() or None
            if "temperature" in _meta:
                try:
                    temperature_override = float(_meta["temperature"])
                except (TypeError, ValueError):
                    pass
            if "pace" in _meta:
                try:
                    pace_override = float(_meta["pace"])
                except (TypeError, ValueError):
                    pass
        except Exception:
            logger.warning("job_metadata_parse_failed, falling back to env/default")

    if not lang:
        lang = os.environ.get("SPEEDCARE_LANG", "").lower() or None

    if not lang or lang not in _SUPPORTED_LANGS:
        if lang:
            logger.warning("unsupported_lang=%s, falling back to en", lang)
        lang = "en"

    # Resolve TTS profile for this language, apply any per-call overrides
    profile = VOICE_PROFILES.get(lang, VOICE_PROFILES["en"])
    effective_voice = voice_override or profile["voice"]
    effective_temperature = temperature_override if temperature_override is not None else profile["temperature"]
    effective_pace = pace_override if pace_override is not None else profile["pace"]

    # STT mode — per-call override or config default
    effective_stt_mode = stt_mode_override or settings.SARVAM_STT_MODE

    logger.info(
        "call_language=%s stt_model=%s stt_mode=%s "
        "tts_voice=%s tts_pace=%.2f tts_temp=%.2f tts_rate=%d dict_id=%s",
        lang, settings.SARVAM_STT_MODEL, effective_stt_mode,
        effective_voice, effective_pace, effective_temperature,
        profile["speech_sample_rate"], _CACHED_DICT_ID or "none",
    )

    agent = SpeedCareAgent(language=lang, voice=effective_voice, db=db, http_client=http)

    # ── VAD endpointing ────────────────────────────────────────────────────
    # Non-English speakers tend to pause longer mid-sentence; give them 50ms
    # more silence budget to avoid premature endpointing.
    silence = (
        settings.AGENT_MIN_SILENCE_EN
        if lang == "en"
        else settings.AGENT_MIN_SILENCE_NON_EN
    )
    turn_handling: TurnHandlingOptions = {
        "endpointing": {
            "min_delay": settings.AGENT_ENDPOINTING_MIN_DELAY,
            "max_delay": settings.AGENT_ENDPOINTING_MAX_DELAY,
        },
        "interruption": {
            "enabled": settings.AGENT_ALLOW_INTERRUPTION,
            "discard_audio_if_uninterruptible": not settings.AGENT_BUFFER_AUDIO_WHILE_SPEAKING,
            "min_duration": settings.AGENT_INTERRUPT_MIN_DURATION,
            "min_words": settings.AGENT_INTERRUPT_MIN_WORDS,
            "resume_false_interruption": True,
            "false_interruption_timeout": settings.AGENT_FALSE_INTERRUPT_TIMEOUT,
        },
    }
    logger.info(
        "turn_tuning lang=%s vad_min_silence=%.2f endpoint_min=%.2f endpoint_max=%.2f interruption_enabled=%s buffer_audio_while_speaking=%s interrupt_min=%.2f interrupt_words=%d false_interrupt_timeout=%.2f preemptive_generation=%s",
        lang,
        silence,
        settings.AGENT_ENDPOINTING_MIN_DELAY,
        settings.AGENT_ENDPOINTING_MAX_DELAY,
        settings.AGENT_ALLOW_INTERRUPTION,
        settings.AGENT_BUFFER_AUDIO_WHILE_SPEAKING,
        settings.AGENT_INTERRUPT_MIN_DURATION,
        settings.AGENT_INTERRUPT_MIN_WORDS,
        settings.AGENT_FALSE_INTERRUPT_TIMEOUT,
        settings.AGENT_PREEMPTIVE_GENERATION,
    )
    session = AgentSession(
        vad=silero.VAD.load(
            min_silence_duration=silence,
            activation_threshold=0.45,   # avoid reacting to tiny bursts / echo
        ),
        preemptive_generation=settings.AGENT_PREEMPTIVE_GENERATION,
        turn_handling=turn_handling,
        stt=SarvamSTT(
            language=lang,
            model=settings.SARVAM_STT_MODEL,
            mode=effective_stt_mode,
            http_client=sarvam_http,
        ),
        llm=_StubLLM(),
        tts=SarvamTTS(
            language=lang,
            voice=effective_voice,
            model="bulbul:v3",
            pace=effective_pace,
            speech_sample_rate=profile["speech_sample_rate"],
            output_codec=profile["output_codec"],
            temperature=effective_temperature,
            dict_id=_CACHED_DICT_ID,
            http_client=sarvam_http,
            # pitch / loudness / enable_preprocessing are bulbul:v2 only —
            # SarvamTTS will NOT send them to the v3 endpoint.
        ),
        aec_warmup_duration=settings.AGENT_AEC_WARMUP_DURATION,
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
