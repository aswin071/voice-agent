from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator
import re


# ── Shared envelope ──

class ResponseEnvelope(BaseModel):
    success: bool = True
    data: Any = None
    error: Any = None
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    retry_after_seconds: int | None = None


# ── Auth ──

class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 900
    user: dict | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class LiveKitTokenRequest(BaseModel):
    call_sid: str
    participant_identity: str
    grants: list[str] = ["roomJoin", "canPublish", "canSubscribe"]
    ttl_seconds: int = 7200


class ApiKeyCreateRequest(BaseModel):
    name: str
    role: str = "service"
    description: str | None = None
    expires_at: datetime | None = None


# ── Booking ──

VEHICLE_NUMBER_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$")


def normalize_vehicle_number(v: str) -> str:
    return re.sub(r"[\s\-]", "", v).upper()


class BookingCreateRequest(BaseModel):
    vehicle_number: str
    service_type: str
    preferred_date: date
    caller_name: str
    caller_number: str | None = None
    call_session_id: uuid.UUID | None = None
    notes: str | None = None

    @field_validator("vehicle_number", mode="before")
    @classmethod
    def validate_vehicle(cls, v: str) -> str:
        normalized = normalize_vehicle_number(v)
        if not VEHICLE_NUMBER_PATTERN.match(normalized):
            raise ValueError(f"Vehicle number '{v}' does not match Indian registration format.")
        return normalized


class BookingResponse(BaseModel):
    booking_id: uuid.UUID
    booking_ref: str
    vehicle_number: str
    service_type: str
    service_label: str | None = None
    appointment_date: date
    appointment_slot: str
    status: str
    customer_name: str
    estimated_duration_minutes: int = 60
    workshop_address: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class BookingRescheduleRequest(BaseModel):
    new_date: date
    reason: str | None = "caller_requested"


class BookingCancelRequest(BaseModel):
    reason: str | None = "caller_cancelled"
    notes: str | None = None


class AvailabilityDateSlot(BaseModel):
    date: date
    slots_remaining: int
    earliest_slot: str


# ── Voice ──

class RoomCreateRequest(BaseModel):
    call_sid: str
    caller_number: str | None = None
    source: str = "sip"
    sip_call_id: str | None = None
    metadata: dict | None = None


class RoomEndRequest(BaseModel):
    reason: str
    outcome: str = "completed"


# ── Agent ──

class ProcessTurnRequest(BaseModel):
    call_session_id: uuid.UUID
    turn_number: int
    transcript: str
    language: str = "ta"
    agent_state: str = "greeting"
    collected_slots: dict = {}


class ProcessTurnResponse(BaseModel):
    response_text: str
    next_agent_state: str
    intent: str | None = None
    updated_slots: dict = {}
    tool_calls_made: list[str] = []
    slots_remaining: list[str] = []
    action: str = "respond"
    llm_latency_ms: int = 0


# ── Notification ──

class NotificationSendRequest(BaseModel):
    booking_id: uuid.UUID
    event_type: str
    recipient_number: str
    language: str = "ta"
    template_vars: dict = {}
    idempotency_key: str


class NotificationResponse(BaseModel):
    notification_id: uuid.UUID
    status: str
    queued_at: datetime | None = None


class TemplateUpdateRequest(BaseModel):
    template: str
    change_reason: str | None = None


# ── Pagination ──

class PaginatedResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[Any]
