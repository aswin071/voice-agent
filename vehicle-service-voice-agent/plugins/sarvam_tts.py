"""Sarvam AI Text-to-Speech plugin for livekit-agents.

Architecture notes (concurrent-user safe):
- Each AgentSession gets its own SarvamTTS instance → no shared mutable state.
- The httpx.AsyncClient is per-instance; limits are tuned for a single call's
  burst (up to 3 concurrent synthesis requests: greeting + 2 back-to-back turns).
- synthesize() returns a new ChunkedStream per call, so parallel calls within a
  session do not share any stream state.
- aclose() is called by AgentSession's cleanup path; never raises.
- dict_id is a read-only string resolved at worker startup — safe under concurrency.

Verified against live Sarvam API (bulbul:v3, 2026-04-13):
- POST https://api.sarvam.ai/text-to-speech
- Body: {text, target_language_code, speaker, model, pace,
         speech_sample_rate, output_audio_codec, temperature, dict_id}
- Response: {request_id, audios: [base64-encoded WAV]}
- v3 native output: 24000 Hz, mono, 16-bit PCM in WAV container
- pitch / loudness / enable_preprocessing are bulbul:v2 ONLY — omitted for v3.
- temperature (expressiveness) and dict_id are bulbul:v3 ONLY.
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

# ---------------------------------------------------------------------------
# Voice catalogue (bulbul:v3 — 30+ speakers, case-sensitive lowercase)
# ---------------------------------------------------------------------------
# Female voices: ritu, priya, neha, pooja, simran, kavya, ishita, shreya,
#                roopa, amelia, sophia, tanya, shruti, suhani, kavitha, rupali
# Male voices  : shubh (default), aditya, rahul, rohan, amit, dev, ratan,
#                varun, manan, sumit, kabir, aayan, ashutosh, advait, anand,
#                tarun, sunny, mani, gokul, vijay, mohit, rehan, soham
#
# "ishita" and "priya" are documented to excel across ALL Indian languages.
# "shubh" is the recommended male voice for multilingual use.
# Defaults below favour a warm, professional female persona.
DEFAULT_VOICES: dict[str, str] = {
    "en": "ishita",
    "ta": "ishita",
    "hi": "ishita",
    "ml": "ishita",
    "te": "ishita",
    "kn": "ishita",
    "mr": "ishita",
    "gu": "ishita",
    "bn": "ishita",
    "pa": "ishita",
    "or": "ishita",
}

# Full v3 female / male lists exposed so the debug UI can enumerate them.
V3_FEMALE_VOICES = [
    "ishita", "priya", "ritu", "neha", "pooja", "simran", "kavya", "shreya",
    "roopa", "amelia", "sophia", "tanya", "shruti", "suhani", "kavitha", "rupali",
]
V3_MALE_VOICES = [
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun",
    "manan", "sumit", "kabir", "aayan", "ashutosh", "advait", "anand", "tarun",
    "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham",
]

# Sarvam expects BCP-47 language codes
LANGUAGE_CODE_MAP: dict[str, str] = {
    "en": "en-IN",
    "ta": "ta-IN",
    "hi": "hi-IN",
    "ml": "ml-IN",
    "te": "te-IN",
    "kn": "kn-IN",
    "mr": "mr-IN",
    "gu": "gu-IN",
    "bn": "bn-IN",
    "pa": "pa-IN",
    "or": "or-IN",
}

# ---------------------------------------------------------------------------
# Audio constants
# bulbul:v3 native output: 24 000 Hz mono 16-bit PCM in WAV container.
# Declaring this as the plugin's native rate lets LiveKit resample only when
# the room's codec actually differs from 24 kHz — no unnecessary resampling.
# ---------------------------------------------------------------------------
SARVAM_SAMPLE_RATE = 24_000
SARVAM_NUM_CHANNELS = 1

# Best-quality settings for different use-cases (all targeting bulbul:v3):
#   pace 1.0  → natural conversational cadence (IVR / bookings)
#   pace 0.85 → measured, clear (accessibility / first-time callers)
# speech_sample_rate 24000 → highest fidelity that the REST endpoint supports
# output_audio_codec "wav" → lossless; LiveKit recompresses for the RTP track
RECOMMENDED_PACE_CONVERSATIONAL = 1.0
RECOMMENDED_PACE_CLEAR = 0.85
RECOMMENDED_SAMPLE_RATE = 24_000


class SarvamTTS(tts.TTS):
    """Sarvam bulbul:v3 TTS plugin for LiveKit AgentSession.

    One instance per call. Thread-safety: all I/O is async; the instance is
    not shared across calls.

    Args:
        language:          Short language code ("en", "ta", "hi", "ml", …).
        voice:             Speaker name from the v3 catalogue. Defaults to
                           DEFAULT_VOICES[language] ("ishita" for all langs).
        pace:              Speech speed. 0.5–2.0. Default 1.0 (natural).
        speech_sample_rate: Output audio sample rate in Hz.
                           One of 8000, 16000, 22050, 24000, 32000, 44100,
                           48000. Default 24000 (highest REST quality).
        output_codec:      Output audio container/codec.
                           One of "wav", "mp3", "aac", "flac", "linear16",
                           "mulaw", "alaw", "opus". Default "wav".
        temperature:       Expressiveness control. 0.01–1.0. Default 0.6.
                           v3 only. Lower = flatter/robotic, higher = lively.
                           0.5 for professional IVR, 0.6–0.7 for warm/natural.
        dict_id:           Pronunciation dictionary ID from Sarvam API.
                           v3 only. Created once at worker startup via
                           `plugins.sarvam_pronunciation.get_or_create_speedcare_dict()`.
                           Pass None to skip (TTS still works without it).
        model:             Sarvam model. "bulbul:v3" (default) or "bulbul:v2".
        api_key:           Override the SARVAM_API_KEY from settings.
        http_client:       Shared httpx.AsyncClient (optional). When None, a
                           per-instance client is created with connection limits
                           appropriate for a single call's burst traffic.
    """

    def __init__(
        self,
        *,
        language: str = "en",
        api_key: str | None = None,
        voice: str | None = None,
        pace: float = RECOMMENDED_PACE_CONVERSATIONAL,
        speech_sample_rate: int = RECOMMENDED_SAMPLE_RATE,
        output_codec: str = "wav",
        temperature: float = 0.6,
        dict_id: str | None = None,
        model: str = "bulbul:v3",
        # v2-only legacy params kept for backward compat; ignored on v3
        pitch: float = 0.0,
        loudness: float = 1.0,
        enable_preprocessing: bool = True,
        http_client: httpx.AsyncClient | None = None,
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SARVAM_SAMPLE_RATE,
            num_channels=SARVAM_NUM_CHANNELS,
        )
        self._api_key = api_key or settings.SARVAM_API_KEY
        self._language = language
        self._voice = voice or DEFAULT_VOICES.get(language, "ishita")
        # Clamp to v3 documented ranges
        self._pace = max(0.5, min(2.0, pace))
        self._speech_sample_rate = speech_sample_rate
        self._output_codec = output_codec
        # v3-only params
        self._temperature = max(0.01, min(1.0, temperature))
        self._dict_id = dict_id or None  # None = omit from payload
        self._model = model
        # v2-only params — stored but only sent for bulbul:v2
        self._pitch = max(-0.75, min(0.75, pitch))
        self._loudness = max(0.3, min(3.0, loudness))
        self._preprocess = enable_preprocessing

        # Per-instance HTTP client with limits suited to one call's burst.
        # Keepalive=60s means the TLS connection is reused across turns in the
        # same call, saving ~50–100 ms per TTS request after the first one.
        self._client = http_client or httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=60.0,
            ),
        )
        self._owns_client = http_client is None

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
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass


class _SarvamChunkedStream(tts.ChunkedStream):
    """Single HTTP round-trip to Sarvam TTS → push WAV into the AudioEmitter.

    The base class handles WAV header stripping and PCM frame chunking when
    mime_type="audio/wav" is declared.  All synthesis parameters come from the
    parent SarvamTTS instance, so this class itself is stateless beyond
    the input text.
    """

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        sarvam: SarvamTTS = self._tts  # type: ignore[assignment]

        target_lang = LANGUAGE_CODE_MAP.get(sarvam._language, "en-IN")
        is_v3 = sarvam._model.startswith("bulbul:v3")

        # Build payload — only include parameters the chosen model supports.
        payload: dict = {
            "text": self._input_text,
            "target_language_code": target_lang,
            "speaker": sarvam._voice,
            "model": sarvam._model,
            "pace": sarvam._pace,
            "speech_sample_rate": sarvam._speech_sample_rate,
            "output_audio_codec": sarvam._output_codec,
        }

        # bulbul:v3-only parameters
        if is_v3:
            payload["temperature"] = sarvam._temperature
            if sarvam._dict_id:
                payload["dict_id"] = sarvam._dict_id

        # bulbul:v2-only parameters — sending them to v3 causes a 400 error.
        if not is_v3:
            payload["pitch"] = sarvam._pitch
            payload["loudness"] = sarvam._loudness
            payload["enable_preprocessing"] = sarvam._preprocess

        logger.debug(
            "[TTS] request lang=%s speaker=%s model=%s pace=%.2f temp=%.2f "
            "rate=%d codec=%s dict_id=%s chars=%d",
            target_lang, sarvam._voice, sarvam._model,
            sarvam._pace, sarvam._temperature if is_v3 else 0,
            sarvam._speech_sample_rate, sarvam._output_codec,
            sarvam._dict_id or "none",
            len(self._input_text),
        )

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
            logger.error("[TTS] request failed: %s", str(e), exc_info=True)
            raise

        if resp.status_code != 200:
            logger.error(
                "[TTS] HTTP %d: %s  payload_keys=%s",
                resp.status_code, resp.text[:300], list(payload.keys()),
            )
            resp.raise_for_status()

        data = resp.json()
        audios = data.get("audios") or []
        if not audios:
            logger.error("[TTS] no audio in response, keys=%s", list(data.keys()))
            return

        request_id = data.get("request_id", "sarvam-tts")

        output_emitter.initialize(
            request_id=request_id,
            sample_rate=SARVAM_SAMPLE_RATE,
            num_channels=SARVAM_NUM_CHANNELS,
            mime_type="audio/wav",
        )

        total_bytes = 0
        for b64_audio in audios:
            wav_bytes = base64.b64decode(b64_audio)
            output_emitter.push(wav_bytes)
            total_bytes += len(wav_bytes)

        output_emitter.flush()
        logger.info(
            "[TTS] done chars=%d audio_bytes=%d request_id=%s speaker=%s lang=%s",
            len(self._input_text), total_bytes, request_id,
            sarvam._voice, target_lang,
        )
