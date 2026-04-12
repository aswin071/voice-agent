"""Sarvam AI Speech-to-Text plugin for livekit-agents.

Architecture notes (concurrent-user safe):
- One SarvamSTT instance per AgentSession (per call). No shared mutable state.
- The httpx.AsyncClient is per-instance with keepalive so TLS is reused across
  turns within the same call (~50-100ms saved after the first request).
- _recognize_impl() is fully async; all local variables — no shared state between
  concurrent calls on different instances.
- Retry logic is per-request, isolated within each call's event loop task.

API: POST https://api.sarvam.ai/speech-to-text
Model: saaras:v3 (recommended, replaces deprecated saarika:v2.5)
Limits: audio file < 30 seconds (REST), formats: WAV/MP3/AAC/FLAC/OGG

Output modes (saaras:v3 only):
  transcribe  — Standard transcription in native script. Default for voice agents.
  codemix     — English words in English, Indic words in native script.
                Best for mixed-language callers (Hinglish, Tanglish).
  translate   — Converts speech to English. NOT used here — agent prompts are
                language-specific; English input breaks language enforcement.
  verbatim    — Word-for-word, preserves spoken numbers. Useful for slot capture.
  translit    — Romanized Latin output. Not needed for this agent.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import wave

import httpx
from livekit.agents import stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.utils import AudioBuffer, merge_frames

from config import get_settings

logger = logging.getLogger("speedcare.sarvam_stt")
settings = get_settings()

# ---------------------------------------------------------------------------
# Language code map — all Sarvam-supported BCP-47 codes + auto-detect
# Specifying the correct code improves accuracy and reduces processing time.
# Pass "unknown" to enable automatic language detection.
# ---------------------------------------------------------------------------
LANGUAGE_CODE_MAP: dict[str, str] = {
    # Primary supported languages
    "en": "en-IN",
    "hi": "hi-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "bn": "bn-IN",
    "pa": "pa-IN",
    "od": "od-IN",
    # Extended languages
    "as": "as-IN",
    "ur": "ur-IN",
    "ne": "ne-IN",
    "kok": "kok-IN",
    "ks": "ks-IN",
    "sd": "sd-IN",
    "sa": "sa-IN",
    "sat": "sat-IN",
    "mni": "mni-IN",
    "brx": "brx-IN",
    "mai": "mai-IN",
    "doi": "doi-IN",
    # Auto-detection sentinel — model returns detected language in response
    "unknown": "unknown",
}

# Valid output modes for saaras:v3
VALID_MODES = frozenset({"transcribe", "translate", "verbatim", "translit", "codemix"})

# Retry configuration
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = (0.5, 1.0)   # delay before retry 1, retry 2
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class SarvamSTT(stt.STT):
    """Sarvam AI STT plugin for LiveKit AgentSession.

    One instance per call. All state is immutable after __init__.

    Args:
        language:    Short language code ("en", "ta", "hi", "ml", "unknown", …).
                     "unknown" enables automatic language detection.
        model:       Sarvam STT model. Default "saaras:v3" (recommended).
                     Do NOT use "saarika:v2.5" — it is deprecated.
        mode:        Output mode for saaras:v3.
                       "transcribe" — native script, number-normalised (default).
                       "codemix"    — English words in English, Indic in native.
                     Other modes (translate, verbatim, translit) are rarely needed
                     for a live voice agent; use Batch API for post-call analytics.
        api_key:     Override SARVAM_API_KEY from settings.
        http_client: Optional shared httpx.AsyncClient. When None, a per-instance
                     client is created (recommended — isolates per-call state).
    """

    def __init__(
        self,
        *,
        language: str = "ta",
        model: str | None = None,
        mode: str | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        self._api_key = api_key or settings.SARVAM_API_KEY

        # Resolve language to BCP-47. If caller passed a full BCP-47 like "ta-IN",
        # keep it as-is; otherwise look up the short code in the map.
        if language in LANGUAGE_CODE_MAP.values() or language == "unknown":
            self._language_bcp47 = language
        else:
            self._language_bcp47 = LANGUAGE_CODE_MAP.get(language, language)

        # Model — prefer config setting, fall back to saaras:v3
        self._model = model or getattr(settings, "SARVAM_STT_MODEL", "saaras:v3")

        # Mode — prefer config setting, validate, fall back to "transcribe"
        _mode = mode or getattr(settings, "SARVAM_STT_MODE", "transcribe")
        if _mode not in VALID_MODES:
            logger.warning("[STT] unsupported mode=%s, falling back to 'transcribe'", _mode)
            _mode = "transcribe"
        self._mode = _mode

        # Per-instance HTTP client.
        # Keepalive=60s → TLS handshake amortised across turns in the same call.
        # max_connections=4 → 1 in-flight STT request + headroom for retries.
        self._client = http_client or httpx.AsyncClient(
            timeout=20.0,
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=60.0,
            ),
        )
        self._owns_client = http_client is None

        logger.info(
            "[STT] initialised model=%s mode=%s language=%s",
            self._model, self._mode, self._language_bcp47,
        )

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        # Per-call language override (e.g., detected mid-call by higher layer).
        # Fall back to the instance's language.
        lang_override = language if (language and language is not NOT_GIVEN) else None
        if lang_override:
            if lang_override in LANGUAGE_CODE_MAP.values() or lang_override == "unknown":
                lang_bcp47 = lang_override
            else:
                lang_bcp47 = LANGUAGE_CODE_MAP.get(lang_override, lang_override)
        else:
            lang_bcp47 = self._language_bcp47

        # ── Build WAV bytes from LiveKit AudioFrames ──────────────────────
        # merge_frames collapses all buffered frames (produced by Silero VAD
        # endpointing) into one contiguous PCM frame. We then wrap it in a WAV
        # container so Sarvam's REST endpoint gets a valid audio file.
        frame = merge_frames(buffer)
        pcm_bytes = bytes(frame.data)

        duration_s = len(pcm_bytes) / (frame.num_channels * 2 * frame.sample_rate)

        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(frame.num_channels)
            wf.setsampwidth(2)          # LiveKit always outputs 16-bit signed PCM
            wf.setframerate(frame.sample_rate)
            wf.writeframes(pcm_bytes)
        audio_bytes = wav_buf.getvalue()

        logger.debug(
            "[STT] audio channels=%d rate=%d duration=%.2fs wav_bytes=%d lang=%s mode=%s",
            frame.num_channels, frame.sample_rate, duration_s,
            len(audio_bytes), lang_bcp47, self._mode,
        )

        # ── POST to Sarvam with retry ─────────────────────────────────────
        last_exc: Exception | None = None
        t0 = time.monotonic()

        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                backoff = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "[STT] retry attempt=%d after %.1fs backoff (prev status=%s)",
                    attempt, backoff,
                    getattr(last_exc, "response", None) and last_exc.response.status_code,  # type: ignore[union-attr]
                )
                await asyncio.sleep(backoff)

            try:
                resp = await self._client.post(
                    settings.SARVAM_STT_URL,
                    headers={"api-subscription-key": self._api_key},
                    files={
                        "file": ("audio.wav", io.BytesIO(audio_bytes), "audio/wav"),
                    },
                    data={
                        "model": self._model,
                        "language_code": lang_bcp47,
                        "mode": self._mode,
                    },
                )

                if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                    continue

                resp.raise_for_status()
                data = resp.json()
                break

            except httpx.HTTPStatusError as e:
                if e.response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                    last_exc = e
                    continue
                # Non-retryable HTTP error (400 / 403 / 422) or exhausted retries
                logger.error(
                    "[STT] http_error status=%d body=%s",
                    e.response.status_code, e.response.text[:200],
                )
                return _empty_event(lang_bcp47)

            except Exception as e:
                logger.error("[STT] request_failed attempt=%d: %s", attempt, str(e), exc_info=True)
                if attempt < _MAX_RETRIES:
                    last_exc = e
                    continue
                return _empty_event(lang_bcp47)

        else:
            # All retries exhausted
            logger.error("[STT] all_retries_exhausted last_error=%s", str(last_exc))
            return _empty_event(lang_bcp47)

        latency_ms = int((time.monotonic() - t0) * 1000)
        transcript: str = data.get("transcript", "")
        detected_lang: str = data.get("language_code", lang_bcp47)
        confidence: float = float(data.get("confidence", 0.9))

        logger.info(
            "[STT] transcript=%r lang=%s conf=%.2f latency_ms=%d model=%s mode=%s",
            transcript, detected_lang, confidence, latency_ms, self._model, self._mode,
        )

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(
                    text=transcript,
                    language=detected_lang,
                    confidence=confidence,
                )
            ],
        )

    async def aclose(self) -> None:
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass


def _empty_event(language: str) -> stt.SpeechEvent:
    """Return a zero-confidence empty transcript — caller continues without STT result."""
    return stt.SpeechEvent(
        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
        alternatives=[stt.SpeechData(text="", language=language, confidence=0.0)],
    )
