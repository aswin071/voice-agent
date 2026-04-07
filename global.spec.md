# global.spec.md — SpeedCare Voice Agent: Global System Specification

---

## Feature: Global System Architecture

### Goal
Define the overarching system design, cross-cutting concerns, shared contracts, technology boundaries, and deployment topology for the SpeedCare Voice Agent — a production-grade, multilingual, inbound voice assistant for a vehicle service business that handles bookings, status queries, and service inquiries via real-time voice calls.

---

## System Overview

```
Caller (SIP/WebRTC)
    │
    ▼
LiveKit SFU (WebRTC Room)
    │  raw audio frames (PCM)
    ▼
Sarvam STT (saaras:v3)  ─── streaming transcript ──▶  Agent Core (Python livekit-agents ~1.4)
                                                              │
                                                    Claude Haiku-4-5 (LLM)
                                                              │  tool calls
                                                    ┌─────────┼─────────┐
                                                    ▼         ▼         ▼
                                               BookingAPI  StatusAPI  NotifyAPI  (FastAPI)
                                                    │
                                              PostgreSQL
                                                    │
                                          Sarvam TTS (bulbul:v3)
                                                    │
                                              LiveKit SFU
                                                    │
                                               Caller (audio)
```

---

## Requirements

- The system SHALL handle inbound voice calls from PSTN (via SIP trunk: Exotel/Plivo) and browser WebRTC.
- The system SHALL support multilingual conversations: Tamil, Malayalam, Hindi, English, and code-mixed variants.
- The system SHALL maintain per-call conversation context (history, collected slots, agent state) in memory for the duration of the call.
- The system SHALL persist call outcomes (booking ref, intent, transcript summary, caller number, timestamps) to PostgreSQL.
- The system SHALL deliver time-to-first-audio (TTFA) under 2 seconds per conversational turn in P95 conditions.
- The system SHALL handle minimum 20 simultaneous inbound calls per agent worker instance without degrading latency.
- The system SHALL expose all backend operations via versioned RESTful FastAPI endpoints (`/api/v1/...`).
- The system SHALL use structured JSON logging (structlog) with correlation IDs propagated across all services.
- The system SHALL enforce mutual TLS between LiveKit and the agent worker.
- The system SHALL use environment-variable-based secrets management; no secrets in source code or images.
- All external API calls (Sarvam, Anthropic, SIP provider) SHALL have retry logic with exponential backoff.
- The system SHALL emit Prometheus metrics from every service for observability.

---

## Technology Stack

| Layer | Component | Version / Notes |
|---|---|---|
| Audio / Realtime | LiveKit Cloud SFU | Cloud-managed |
| Audio / Realtime | Silero VAD | Bundled with livekit-agents |
| Audio / Realtime | LiveKit Noise Cancel | Plugin |
| Audio / Realtime | LiveKit Turn Detector | Plugin |
| STT | Sarvam saaras:v3 | Streaming, multilingual |
| LLM | Anthropic Claude Haiku-4-5 (`claude-haiku-4-5`) | Tool use enabled |
| TTS | Sarvam bulbul:v3 | Streaming synthesis |
| Agent Framework | livekit-agents ~1.4 | Python |
| Backend API | FastAPI | Python ≥ 3.10 |
| ORM | SQLAlchemy 2.x async | With alembic migrations |
| Database | PostgreSQL 15 | Primary datastore |
| Cache / Session | Redis 7 | Call session state, rate limiting |
| Notifications | Twilio SMS / Exotel SMS | Booking confirmations |
| Package Manager | uv | Python |
| Container | Docker + Docker Compose | Production + local dev |
| Observability | Prometheus + Grafana | Metrics |
| Error Tracking | Sentry | Runtime errors |
| CI | GitHub Actions | Lint, test, build |

---

## Latency Budgets (P95 per conversational turn)

| Stage | Target | Hard Limit |
|---|---|---|
| VAD endpointing (speech-end detect) | 70 ms | 150 ms |
| STT transcript (streaming final) | 400 ms | 700 ms |
| LLM first token | 800 ms | 1,200 ms |
| TTS first audio chunk | 300 ms | 500 ms |
| **Total TTFA (end-to-end)** | **< 2,000 ms** | **3,000 ms** |

---

## API Contract: Global

### Base URL
```
https://api.speedcare.in/api/v1
```

### Shared Request Headers
```
Content-Type: application/json
X-Request-ID: <uuid4>          # Required on all requests; propagated in logs
X-Caller-Number: <E.164>       # Optional; populated by telephony layer
Authorization: Bearer <jwt>    # Required for protected endpoints
```

### Shared Response Envelope
```json
{
  "success": true,
  "data": { },
  "error": null,
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2026-03-25T10:00:00Z"
}
```

### Error Envelope
```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "BOOKING_SLOT_UNAVAILABLE",
    "message": "The requested time slot is no longer available.",
    "field": "appointment_datetime",
    "retry_after_seconds": null
  },
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2026-03-25T10:00:00Z"
}
```

### Standard HTTP Status Codes
| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Resource created |
| 400 | Validation error |
| 401 | Unauthenticated |
| 403 | Forbidden |
| 404 | Resource not found |
| 409 | Conflict (duplicate, slot taken) |
| 422 | Unprocessable entity |
| 429 | Rate limit exceeded |
| 500 | Internal server error |
| 503 | Upstream dependency unavailable |

---

## Data Model: Shared / Cross-Cutting

### `call_sessions` (master call log)
```sql
CREATE TABLE call_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_sid        VARCHAR(64) UNIQUE NOT NULL,   -- LiveKit/SIP call identifier
    caller_number   VARCHAR(20),                    -- E.164 format
    language        VARCHAR(10) DEFAULT 'ta',       -- ISO 639-1
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    duration_seconds INT,
    intent          VARCHAR(50),                    -- booking_new | booking_status | general_inquiry
    agent_state     VARCHAR(30) DEFAULT 'greeting', -- greeting | collecting | confirming | closing
    booking_id      UUID REFERENCES bookings(id),
    outcome         VARCHAR(30),                    -- completed | failed | abandoned | transferred
    transcript_url  TEXT,
    error_log       JSONB,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_call_sessions_caller ON call_sessions(caller_number);
CREATE INDEX idx_call_sessions_started ON call_sessions(started_at DESC);
```

### `audit_logs`
```sql
CREATE TABLE audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id   UUID NOT NULL,
    action      VARCHAR(50) NOT NULL,
    actor_id    UUID,
    actor_type  VARCHAR(20) DEFAULT 'system',
    diff        JSONB,
    ip_address  INET,
    request_id  UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_entity ON audit_logs(entity_type, entity_id);
```

---

## Business Logic: System Boot & Graceful Shutdown

1. On startup, agent worker reads env vars; validates all required keys are present — fails fast if missing.
2. Establishes Redis connection pool (min 5, max 20 connections).
3. Establishes PostgreSQL async connection pool (min 3, max 15).
4. Registers LiveKit agent worker with room dispatcher.
5. On SIGTERM, agent worker:
   a. Stops accepting new calls.
   b. Waits up to 60 seconds for in-progress calls to complete.
   c. Flushes pending DB writes.
   d. Closes all connection pools.
   e. Exits with code 0.

## Business Logic: Request Correlation

- Every inbound HTTP request and every LiveKit room event generates a UUID v4 `request_id`.
- `request_id` is attached to the structlog context for the lifetime of the request.
- `request_id` is forwarded in `X-Request-ID` header on all outbound sub-requests.
- `request_id` is stored in the `call_sessions.metadata` JSON at call end.

## Business Logic: Retry Policy (default unless overridden per module)

```
Max attempts : 3
Backoff      : exponential (base 200ms, multiplier 2, jitter ±20%)
Retry on     : HTTP 429, 502, 503, 504; network timeout; connection reset
Do NOT retry : HTTP 400, 401, 403, 404, 409, 422
```

---

## Edge Cases

| Scenario | Handling |
|---|---|
| All three LLM retry attempts fail | Respond with pre-canned TTS fallback ("I'm having trouble understanding, please call back"); log to Sentry |
| Sarvam STT returns empty transcript | Treat as silence; increment silence counter; apply silence timeout logic |
| Database connection pool exhausted | Return 503; agent falls back to in-memory session only; alert ops |
| Redis unavailable | Degrade gracefully: skip distributed rate limiting, use local in-memory rate limiter |
| LiveKit room disconnects mid-call | Mark call `outcome=abandoned`; flush partial session to DB |
| Duplicate `call_sid` detected | Reject second connection; log conflict; return 409 |

---

## Constraints

- **Concurrency**: Each agent worker process handles up to 20 concurrent LiveKit rooms; horizontal scaling via Docker replicas.
- **Statelessness**: FastAPI service nodes are stateless; all session state lives in Redis or DB.
- **Idempotency**: All booking creation and notification endpoints accept an `idempotency_key` (UUID); duplicate requests within 24h return the original response.
- **Data Residency**: All call data stored in India-region PostgreSQL instance (compliance with TRAI regulations).
- **PII Masking**: Caller numbers masked in application logs (`+91 XXXXX X1234`); full number only in DB with encryption-at-rest.
- **Secrets**: API keys stored in `.env` (local) / AWS Secrets Manager (production); rotated quarterly.
- **Rate Limiting**: 60 API requests/minute per IP on public endpoints; 600/minute for internal service-to-service.

---

## Acceptance Criteria

- [ ] System passes end-to-end smoke test: inbound WebRTC call → booking created → SMS confirmation delivered.
- [ ] P95 TTFA measured under simulated load of 10 concurrent calls is < 2,000 ms.
- [ ] All services start cleanly from `docker compose up` with only a valid `.env` file.
- [ ] SIGTERM triggers graceful shutdown without data loss on in-progress calls.
- [ ] All structured logs contain `request_id`, `call_sid`, and `timestamp` fields.
- [ ] Sentry captures all unhandled exceptions within 30 seconds of occurrence.
- [ ] Zero secrets present in Docker images or source code (verified by `truffleHog` scan).
- [ ] Prometheus endpoint `/metrics` reachable on all services.
