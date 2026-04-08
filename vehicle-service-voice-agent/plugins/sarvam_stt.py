from __future__ import annotations

import io
import logging
import wave
from dataclasses import dataclass

import httpx
from livekit.agents import stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr
from livekit.agents.utils import AudioBuffer, merge_frames

from config import get_settings

logger = logging.getLogger("speedcare.sarvam_stt")
settings = get_settings()


@dataclass
class SarvamSTTOptions:
    language: str = "ta"
    model: str = "saarika:v2.5"


class SarvamSTT(stt.STT):
    """Sarvam AI Speech-to-Text plugin for livekit-agents.

    Uses the dedicated transcription endpoint (`speech-to-text`) with
    `saarika:v2.5` — the modern fast STT model. We do NOT use the
    `speech-to-text-translate` endpoint because:
      1. It forces output to English even when the caller speaks Tamil/Hindi,
         so a prompt like "respond ONLY in Tamil" sees English input and
         the agent ends up confused mid-call.
      2. It is ~130 ms slower per turn on average.
    """

    def __init__(self, *, language: str = "ta", api_key: str | None = None, model: str = "saarika:v2.5"):
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        self._api_key = api_key or settings.SARVAM_API_KEY
        self._language = language
        self._model = model
        self._client = httpx.AsyncClient(timeout=10)

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        lang = language if language and language is not NOT_GIVEN else self._language

        # Merge incoming AudioFrame(s) into one PCM frame, then wrap as a WAV file
        # so the Sarvam HTTP endpoint receives a valid audio container.
        frame = merge_frames(buffer)
        pcm_bytes = bytes(frame.data)

        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(frame.num_channels)
            wf.setsampwidth(2)  # 16-bit signed PCM (LiveKit default)
            wf.setframerate(frame.sample_rate)
            wf.writeframes(pcm_bytes)
        audio_bytes = wav_buf.getvalue()

        try:
            resp = await self._client.post(
                settings.SARVAM_STT_URL,
                headers={
                    "api-subscription-key": self._api_key,
                },
                files={
                    "file": ("audio.wav", io.BytesIO(audio_bytes), "audio/wav"),
                },
                data={
                    # saarika expects BCP-47 codes (ta-IN, hi-IN, ...). Map common short codes.
                    "language_code": {"ta": "ta-IN", "hi": "hi-IN", "ml": "ml-IN", "en": "en-IN"}.get(lang, lang),
                    "model": self._model,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            transcript = data.get("transcript", "")
            detected_lang = data.get("language_code", lang)
            confidence = data.get("confidence", 0.9)

            logger.info("[STT] transcript=%r lang=%s conf=%.2f", transcript, detected_lang, confidence)

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
        except Exception as e:
            logger.error("[STT] sarvam_stt_error: %s", str(e), exc_info=True)
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[
                    stt.SpeechData(text="", language=lang, confidence=0.0)
                ],
            )
