from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, Time, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db import Base


# ── call_sessions ──
class CallSession(Base):
    __tablename__ = "call_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_sid = Column(String(64), unique=True, nullable=False)
    caller_number = Column(String(20))
    language = Column(String(10), default="ta")
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Integer)
    intent = Column(String(50))
    agent_state = Column(String(30), default="greeting")
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"))
    outcome = Column(String(30))
    transcript_url = Column(Text)
    error_log = Column(JSONB)
    metadata_ = Column("metadata", JSONB, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    voice_turns = relationship("VoiceTurn", back_populates="call_session")
    agent_turns = relationship("AgentTurn", back_populates="call_session")

    __table_args__ = (
        Index("idx_call_sessions_caller", "caller_number"),
        Index("idx_call_sessions_started", started_at.desc()),
    )


# ── bookings ──
class Booking(Base):
    __tablename__ = "bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_ref = Column(String(20), unique=True, nullable=False)
    vehicle_number = Column(String(15), nullable=False)
    service_type = Column(String(50), nullable=False)
    appointment_date = Column(Date, nullable=False)
    appointment_slot = Column(Time, nullable=False, default=time(9, 0))
    status = Column(String(20), nullable=False, default="pending")
    customer_name = Column(String(100), nullable=False)
    caller_number = Column(String(20))
    call_session_id = Column(UUID(as_uuid=True), ForeignKey("call_sessions.id"))
    notes = Column(Text)
    estimated_duration_minutes = Column(Integer, default=60)
    idempotency_key = Column(UUID(as_uuid=True), unique=True)
    cancelled_at = Column(DateTime(timezone=True))
    cancellation_reason = Column(Text)
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    status_history = relationship("BookingStatusHistory", back_populates="booking")
    notifications = relationship("Notification", back_populates="booking")

    __table_args__ = (
        Index("idx_bookings_vehicle", "vehicle_number"),
        Index("idx_bookings_date", "appointment_date"),
        Index("idx_bookings_status", "status"),
        Index("idx_bookings_ref", "booking_ref"),
    )


class BookingStatusHistory(Base):
    __tablename__ = "booking_status_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    old_status = Column(String(20))
    new_status = Column(String(20), nullable=False)
    changed_by = Column(UUID(as_uuid=True))
    reason = Column(Text)
    changed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking = relationship("Booking", back_populates="status_history")

    __table_args__ = (
        Index("idx_bsh_booking", "booking_id"),
    )


class DailyCapacity(Base):
    __tablename__ = "daily_capacity"

    date = Column(Date, primary_key=True)
    total_slots = Column(Integer, nullable=False, default=20)
    booked_slots = Column(Integer, nullable=False, default=0)
    is_holiday = Column(Boolean, default=False)
    notes = Column(Text)


class ServiceCatalog(Base):
    __tablename__ = "service_catalog"

    code = Column(String(50), primary_key=True)
    label = Column(String(100), nullable=False)
    label_ta = Column(String(100))
    label_hi = Column(String(100))
    label_ml = Column(String(100))
    estimated_duration_min = Column(Integer, nullable=False, default=60)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)


# ── voice_turns ──
class VoiceTurn(Base):
    __tablename__ = "voice_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_session_id = Column(UUID(as_uuid=True), ForeignKey("call_sessions.id", ondelete="CASCADE"), nullable=False)
    turn_number = Column(Integer, nullable=False)
    direction = Column(String(10), nullable=False)
    raw_audio_start_ms = Column(Integer)
    vad_end_ms = Column(Integer)
    stt_start_ms = Column(Integer)
    stt_final_ms = Column(Integer)
    llm_start_ms = Column(Integer)
    llm_first_token_ms = Column(Integer)
    tts_start_ms = Column(Integer)
    tts_first_chunk_ms = Column(Integer)
    audio_delivered_ms = Column(Integer)
    transcript = Column(Text)
    agent_response = Column(Text)
    stt_confidence = Column(Float)
    language = Column(String(10))
    barge_in_occurred = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    call_session = relationship("CallSession", back_populates="voice_turns")

    __table_args__ = (
        Index("idx_voice_turns_session", "call_session_id", "turn_number"),
    )


# ── agent_turns ──
class AgentTurn(Base):
    __tablename__ = "agent_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_session_id = Column(UUID(as_uuid=True), ForeignKey("call_sessions.id", ondelete="CASCADE"), nullable=False)
    turn_number = Column(Integer, nullable=False)
    transcript = Column(Text, nullable=False)
    intent_classified = Column(String(50))
    confidence = Column(Float)
    agent_response = Column(Text, nullable=False)
    agent_state_before = Column(String(30))
    agent_state_after = Column(String(30))
    slots_before = Column(JSONB)
    slots_after = Column(JSONB)
    tool_calls = Column(JSONB, server_default=text("'[]'::jsonb"))
    llm_model = Column(String(50), default="claude-haiku-4-5")
    llm_input_tokens = Column(Integer)
    llm_output_tokens = Column(Integer)
    llm_latency_ms = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    call_session = relationship("CallSession", back_populates="agent_turns")

    __table_args__ = (
        Index("idx_agent_turns_session", "call_session_id", "turn_number"),
    )


# ── stt_errors ──
class STTError(Base):
    __tablename__ = "stt_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_session_id = Column(UUID(as_uuid=True), ForeignKey("call_sessions.id"), nullable=False)
    turn_number = Column(Integer)
    error_type = Column(String(50))
    raw_response = Column(JSONB)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── auth: users ──
class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="operator")
    is_active = Column(Boolean, default=True)
    last_login_at = Column(DateTime(timezone=True))
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_users_email", "email"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    key_prefix = Column(String(20), nullable=False)
    hashed_key = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="service")
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    expires_at = Column(DateTime(timezone=True))
    last_used_at = Column(DateTime(timezone=True))
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(255), unique=True, nullable=False)
    issued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    ip_address = Column(INET)
    user_agent = Column(Text)


# ── notifications ──
class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    event_type = Column(String(50), nullable=False)
    recipient_number = Column(String(20), nullable=False)
    language = Column(String(10), nullable=False, default="ta")
    status = Column(String(20), nullable=False, default="queued")
    provider = Column(String(20))
    provider_message_id = Column(String(100))
    rendered_message = Column(Text, nullable=False)
    idempotency_key = Column(String(200), unique=True, nullable=False)
    retry_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True))
    queued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    failed_at = Column(DateTime(timezone=True))
    error_detail = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    booking = relationship("Booking", back_populates="notifications")

    __table_args__ = (
        Index("idx_notifications_booking", "booking_id"),
        Index("idx_notifications_idempotent", "idempotency_key"),
    )


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    id = Column(String(100), primary_key=True)
    event_type = Column(String(50), nullable=False)
    language = Column(String(10), nullable=False)
    template = Column(Text, nullable=False)
    char_count = Column(Integer)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("event_type", "language"),
    )


# ── audit_logs ──
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(String(50), nullable=False)
    actor_id = Column(UUID(as_uuid=True))
    actor_type = Column(String(20), default="system")
    diff = Column(JSONB)
    ip_address = Column(INET)
    request_id = Column(UUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_audit_entity", "entity_type", "entity_id"),
    )
