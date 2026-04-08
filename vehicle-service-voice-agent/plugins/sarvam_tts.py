"""Sarvam AI Text-to-Speech plugin for livekit-agents.

Implements the non-streaming `synthesize()` -> `ChunkedStream` contract that
LiveKit's AgentSession expects when `TTSCapabilities(streaming=False)`.
LiveKit automatically wraps this with a StreamAdapter when the pipeline
needs streaming output.

Verified against the live Sarvam TTS API on 2026-04-07:
- POST https://api.sarvam.ai/text-to-speech
- Body: {text, target_language_code, speaker, model}
- Response: {request_id, audios: [base64-encoded WAV]}
- Audio format: 22050 Hz, mono, 16-bit PCM in WAV container
"""
from __future__ import annotations

import base64
import logging

import httpx
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

from config import get_settings

logger = logging.getLogger("speedcare.sarvam_tts")
settings = get_settings()

# Sarvam bulbul:v3 speakers. v3 is the modern, much more natural model
# (verified against the live API on 2026-04-08). v3 ships with a different
# speaker pool than v2 — DO NOT mix v2 names like "anushka" with model="bulbul:v3"
# or the API rejects with "Speaker X is not compatible with model bulbul:v3".
#
# Picked per language for warmth/conversational feel. Override per call with voice=...
DEFAULT_VOICES = {
    "ta": "priya",   # warm Tamil female
    "hi": "priya",   # conversational Hindi female
    "en": "priya",   # warm Indian-English female
    "ml": "priya",   # warm Malayalam female
    "te": "priya",
    "kn": "priya",
    "mr": "priya",
    "gu": "priya",
    "bn": "priya",
}

# Sarvam expects BCP-47 style language codes (en-IN, ta-IN, etc.)
LANGUAGE_CODE_MAP = {
    "en": "en-IN",
    "ta": "ta-IN",
    "hi": "hi-IN",
    "ml": "ml-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "bn": "bn-IN",
}

# Sarvam returns 22050 Hz mono 16-bit WAV. We declare this as the plugin's
# native sample rate; LiveKit will resample to whatever the room needs.
SARVAM_SAMPLE_RATE = 22050
SARVAM_NUM_CHANNELS = 1


class SarvamTTS(tts.TTS):
    def __init__(
        self,
        *,
        language: str = "en",
        api_key: str | None = None,
        voice: str | None = None,
        pace: float = 0.95,
        pitch: float = 0.0,
        loudness: float = 1.3,
        enable_preprocessing: bool = True,
        model: str = "bulbul:v3",
        http_client: httpx.AsyncClient | None = None,
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SARVAM_SAMPLE_RATE,
            num_channels=SARVAM_NUM_CHANNELS,
        )
        self._api_key = api_key or settings.SARVAM_API_KEY
        self._language = language
        self._voice = voice or DEFAULT_VOICES.get(language, "anushka")
        # Naturalness knobs — clamped to Sarvam's documented ranges.
        self._pace = max(0.5, min(2.0, pace))
        self._pitch = max(-0.75, min(0.75, pitch))
        self._loudness = max(0.3, min(3.0, loudness))
        # enable_preprocessing tells Sarvam to expand abbreviations, spell out
        # numbers/dates/plate codes, and normalize punctuation. Without this
        # the agent reads "TN09AK1234" and "₹500" as a robotic letter-stream.
        self._preprocess = enable_preprocessing
        self._model = model
        self._client = http_client or httpx.AsyncClient(timeout=30)

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "_SarvamChunkedStream":
        return _SarvamChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class _SarvamChunkedStream(tts.ChunkedStream):
    """One-shot HTTP request to Sarvam, then push the WAV bytes into the
    AudioEmitter. The base class handles WAV decoding and frame chunking
    automatically when given mime_type=audio/wav."""

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        sarvam: SarvamTTS = self._tts  # type: ignore[assignment]

        target_lang = LANGUAGE_CODE_MAP.get(sarvam._language, "en-IN")
        payload = {
            "text": self._input_text,
            "target_language_code": target_lang,
            "speaker": sarvam._voice,
            "model": sarvam._model,
            "pace": sarvam._pace,
            "enable_preprocessing": sarvam._preprocess,
        }
        # bulbul:v3 rejects pitch/loudness with a 400. Only send them on v2.
        if not sarvam._model.startswith("bulbul:v3"):
            payload["pitch"] = sarvam._pitch
            payload["loudness"] = sarvam._loudness

        try:
            resp = await sarvam._client.post(
                settings.SARVAM_TTS_URL,
                headers={
                    "api-subscription-key": sarvam._api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except Exception as e:
            logger.error("sarvam_tts_request_failed", extra={"error": str(e)})
            raise

        if resp.status_code != 200:
            logger.error(
                "sarvam_tts_http_error",
                extra={"status": resp.status_code, "body": resp.text[:300]},
            )
            resp.raise_for_status()

        data = resp.json()
        audios = data.get("audios") or []
        if not audios:
            logger.error("sarvam_tts_no_audio", extra={"keys": list(data.keys())})
            return

        request_id = data.get("request_id", "sarvam-tts")

        output_emitter.initialize(
            request_id=request_id,
            sample_rate=SARVAM_SAMPLE_RATE,
            num_channels=SARVAM_NUM_CHANNELS,
            mime_type="audio/wav",
        )

        # Sarvam may return multiple audio chunks if the text was long.
        # Push them all into the emitter sequentially.
        for b64_audio in audios:
            wav_bytes = base64.b64decode(b64_audio)
            output_emitter.push(wav_bytes)

        output_emitter.flush()
        logger.info(
            "sarvam_tts_synthesized",
            extra={
                "chars": len(self._input_text),
                "audio_bytes": sum(len(base64.b64decode(a)) for a in audios),
                "request_id": request_id,
            },
        )
