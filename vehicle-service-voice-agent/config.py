from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LiveKit
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""

    # Sarvam AI
    SARVAM_API_KEY: str = ""
    SARVAM_STT_URL: str = "https://api.sarvam.ai/speech-to-text-translate"
    SARVAM_TTS_URL: str = "https://api.sarvam.ai/text-to-speech"

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-haiku-4-5"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_OUTPUT_TOKENS: int = 150

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://speedcare:speedcare@localhost:5432/speedcare"
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

    # App
    APP_NAME: str = "SpeedCare Voice Agent"
    WORKSHOP_ADDRESS: str = "SpeedCare, 45 Anna Salai, Chennai 600002"
    DAILY_BOOKING_CAPACITY: int = 20
    BOOKING_WINDOW_DAYS: int = 30

    # Sentry
    SENTRY_DSN: str = ""

    class Config:
        env_file = ".env.local"
        case_sensitive = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
