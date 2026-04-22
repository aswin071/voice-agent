from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LiveKit
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""

    # Sarvam AI
    SARVAM_API_KEY: str = ""
    SARVAM_STT_URL: str = "https://api.sarvam.ai/speech-to-text"
    SARVAM_TTS_URL: str = "https://api.sarvam.ai/text-to-speech"

    # STT — saaras:v3 is the recommended model (saarika:v2.5 is deprecated).
    # mode: transcribe (default) | codemix | verbatim | translate | translit
    #   transcribe — native script, number-normalised. Best for most callers.
    #   codemix    — English words in English, Indic in native script.
    #                Override per-call via dispatch metadata: {"stt_mode":"codemix"}
    SARVAM_STT_MODEL: str = "saaras:v3"
    SARVAM_STT_MODE: str = "transcribe"

    # Pronunciation dictionary — set this after first worker startup so the
    # worker reuses the existing dict instead of creating a new one each time.
    # Sarvam limits accounts to 10 dictionaries total.
    # Example: SARVAM_DICT_ID=p_5cb7faa6
    SARVAM_DICT_ID: str = ""

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-4-5-20251001"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_OUTPUT_TOKENS: int = 100
    LLM_HTTP_TIMEOUT: float = 8.0

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://admin:admin@localhost:5432/speedcare"
    DB_POOL_MIN: int = 3
    DB_POOL_MAX: int = 15

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_POOL_MIN: int = 5
    REDIS_POOL_MAX: int = 20

    # Auth
    JWT_SECRET: str = "change-me-in-production-min-256-bit"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # SMS
    SMS_PROVIDER: str = "exotel"  # exotel | twilio
    EXOTEL_API_KEY: str = ""
    EXOTEL_API_TOKEN: str = ""
    EXOTEL_SID: str = ""
    EXOTEL_SENDER_NUMBER: str = ""
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    # SIP
    SIP_OUTBOUND_TRUNK_ID: str = ""

    # CORS — comma-separated list of allowed origins
    # Example: "https://app.speedcare.in,https://admin.speedcare.in"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:8000"
    ALLOW_SAME_HOST_CORS: bool = True

    # App
    APP_NAME: str = "SpeedCare Voice Agent"
    WORKSHOP_ADDRESS: str = "SpeedCare, 45 Anna Salai, Chennai 600002"
    DAILY_BOOKING_CAPACITY: int = 20
    BOOKING_WINDOW_DAYS: int = 30

    # Voice pipeline tuning
    AGENT_ENDPOINTING_MIN_DELAY: float = 0.45
    AGENT_ENDPOINTING_MAX_DELAY: float = 1.8
    AGENT_MIN_SILENCE_EN: float = 0.40
    AGENT_MIN_SILENCE_NON_EN: float = 0.55
    AGENT_INTERRUPT_MIN_DURATION: float = 0.8
    AGENT_INTERRUPT_MIN_WORDS: int = 2
    AGENT_FALSE_INTERRUPT_TIMEOUT: float = 1.2
    AGENT_AEC_WARMUP_DURATION: float = 4.0
    AGENT_ALLOW_INTERRUPTION: bool = False
    AGENT_PREEMPTIVE_GENERATION: bool = False
    AGENT_BUFFER_AUDIO_WHILE_SPEAKING: bool = True

    # Sentry
    SENTRY_DSN: str = ""

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_database_url(cls, v: str) -> str:
        # Railway (and many PaaS) provide postgresql:// or postgres://
        # but SQLAlchemy asyncpg driver requires postgresql+asyncpg://
        if isinstance(v, str):
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+asyncpg://", 1)
            if v.startswith("postgresql://") and "+asyncpg" not in v:
                return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    class Config:
        env_file = ".env.local"
        case_sensitive = True
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
