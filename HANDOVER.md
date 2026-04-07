# SpeedCare Voice Agent - Development Handover

**Date:** 2026-04-06
**Session:** Initial Analysis and Core Implementation

**Status:** Core API and Agent infrastructure completed. Ready for agent worker implementation and testing.

---

## Summary of Changes Made

### New Files Created

1. **`plugins/sarvam_tts.py`** - Sarvam AI TTS plugin with streaming support
   - Implements livekit-agents TTS interface
   - Language-aware voice selection (meera for Tamil, mia for English, etc.)
   - Automatic text chunking at sentence boundaries for long inputs
   - Speed control support

2. **`api/routers/agent.py`** - Agent API endpoints
   - `POST /api/v1/agent/process-turn` - Process conversation turn
   - `GET /api/v1/agent/sessions/{id}/context` - Get session context from Redis/DB
   - `POST /api/v1/agent/sessions/{id}/override` - Operator state override
   - `GET /api/v1/agent/turns` - List agent turns with filtering

3. **`main.py`** - FastAPI application entry point
   - Complete FastAPI app with lifespan management
   - Prometheus metrics at `/metrics`
   - Health/readiness/liveness probes
   - Structured logging with structlog
   - Request ID propagation and timing
   - Error handling middleware

### Modified Files

1. **`plugins/__init__.py`** - Added exports for SarvamSTT and SarvamTTS

2. **`agent_core/tools.py`** - Added async tool handlers
   - `async_create_booking()` - Creates booking via booking_service
   - `async_lookup_booking_status()` - Queries booking by ref or vehicle

3. **`agent_core/state_machine.py`** - Enhanced with DB integration
   - Added `extract_caller_name()` function with pattern matching
   - Updated `ConversationalAgent` to accept `db` parameter
   - `_booking_turn` now extracts caller name and handles booking lookup
   - `_confirmation_turn` now creates bookings via database

4. **`api/routers/__init__.py`** - Added router exports

5. **`requirements.txt`** - Complete dependency list added

---

## Project Overview

SpeedCare Voice Agent is a multilingual (Tamil, Malayalam, Hindi, English), real-time voice assistant for vehicle service bookings. It handles:
- Inbound voice calls via SIP/WebRTC through LiveKit
- Conversational AI using Claude Haiku-4-5
- STT via Sarvam saaras:v3
- TTS via Sarvam bulbul:v3
- Booking management via FastAPI backend

---

## Directory Structure

```
vehicle-service-voice-agent/
├── agent_core/           # Conversational agent logic
│   ├── prompts.py        # System prompts and message templates
│   ├── session.py        # Redis session management
│   ├── state_machine.py  # 3-stage agent (Greeting→Booking→Confirmation)
│   └── tools.py          # Tool definitions and handlers for Claude
├── api/                  # FastAPI backend
│   ├── models.py         # SQLAlchemy models (18 tables)
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── deps.py           # Dependencies (auth, Redis, request ID)
│   ├── routers/          # API endpoints
│   │   ├── auth.py
│   │   ├── bookings.py
│   │   ├── notifications.py
│   │   └── voice.py
│   └── services/         # Business logic
│       ├── auth_service.py
│       ├── booking_service.py
│       └── notification_service.py
├── db/                   # Database setup
│   └── __init__.py       # Async SQLAlchemy engine and session
├── plugins/              # LiveKit plugins for STT/TTS
│   ├── __init__.py       # (empty - needs exports)
│   ├── sarvam_stt.py     # Sarvam STT plugin (implemented)
│   └── sarvam_tts.py     # (MISSING - needs implementation)
├── config.py             # Pydantic settings
└── agent.py              # (LEGACY - LiveKit demo, needs replacement)
```

---

## Current State

### ✅ Completed Files

1. **Database Models** (`api/models.py`)
   - 18 tables: call_sessions, bookings, voice_turns, agent_turns, notifications, etc.
   - Proper indexes and relationships
   - Support for audit logging and idempotency

2. **API Layer** (`api/`)
   - Full CRUD for bookings
   - Voice room management endpoints
   - Auth with JWT and API keys
   - Notification service with SMS (Exotel/Twilio)
   - Response envelope standardization

3. **Agent Core** (`agent_core/`)
   - Complete state machine with 3 stages
   - Prompts in 4 languages (ta, hi, en, ml)
   - Session management in Redis (4-hour TTL)
   - Tool definitions for Claude

4. **STT Plugin** (`plugins/sarvam_stt.py`)
   - Implements livekit-agents STT interface
   - Integrates with Sarvam saaras:v3 API
   - Supports streaming transcription

5. **Tool Functions** (`agent_core/tools.py`)
   - `normalize_vehicle_number` - Indian vehicle number validation
   - `validate_date` - Date parsing with relative terms (tomorrow, next Monday)
   - `check_service_type` - Service description to code mapping
   - Tool definitions for Claude tool use

### ⚠️ Partially Complete

1. **Tool Handlers** (`agent_core/tools.py` line 205+)
   - Missing: `create_booking` handler (only has definition)
   - Missing: `lookup_booking_status` handler
   - These need to integrate with booking_service

2. **State Machine** (`agent_core/state_machine.py` line 120-122)
   - `caller_name` extraction not implemented
   - Currently passes silently when name is in transcript

### ❌ Missing Components

1. **Sarvam TTS Plugin** (`plugins/sarvam_tts.py`)
   - Need streaming TTS implementation for bulbul:v3
   - Must implement livekit-agents TTS interface

2. **Main Application Entry** (`main.py`)
   - FastAPI app factory
   - Startup/shutdown lifecycle
   - Health check endpoints
   - Prometheus metrics

3. **Agent Router** (`api/routers/agent.py`)
   - `POST /api/v1/agent/process-turn` endpoint
   - `GET /api/v1/agent/sessions/{id}/context` endpoint
   - `POST /api/v1/agent/sessions/{id}/override` endpoint

4. **Plugins Exports** (`plugins/__init__.py`)
   - Currently empty, needs to export STT and TTS classes

5. **Agent Worker** (replacement for `agent.py`)
   - LiveKit agent worker implementation
   - Pipeline wiring: VAD → STT → Agent → TTS
   - Barge-in and silence timeout handling

---

## Specification References

All implementation should follow the `.spec.md` files:
- `global.spec.md` - System architecture, shared contracts
- `agent/agent.spec.md` - Conversational agent state machine
- `voice/voice.spec.md` - Real-time voice pipeline
- `booking/booking.spec.md` - Booking service requirements
- `auth/auth.spec.md` - Authentication specs
- `notification/notification.spec.md` - SMS notification specs

---

## Next Priority Tasks

### 1. Implement Sarvam TTS Plugin (Highest Priority)
**File:** `plugins/sarvam_tts.py`
**Requirements:**
- Implement livekit-agents TTS interface
- Use Sarvam bulbul:v3 API
- Support streaming audio chunks
- Language-aware voice selection

**API Endpoint:** `https://api.sarvam.ai/text-to-speech`
**Reference:** See `sarvam_stt.py` for HTTP client pattern

### 2. Complete Tool Handlers
**File:** `agent_core/tools.py`
**Add to TOOL_HANDLERS dict:**
```python
"create_booking": lambda args: create_booking_handler(args)
"lookup_booking_status": lambda args: lookup_booking_handler(args)
```
**Note:** These need async support - the current lambda pattern won't work for DB calls. Consider making `_call_llm` handle async tool execution.

### 3. Create Main FastAPI Application
**File:** `main.py`
**Requirements:**
- FastAPI app with all routers mounted
- Startup: connect to Redis and PostgreSQL
- Shutdown: close connections gracefully
- Health check at `/health`
- Metrics at `/metrics`

### 4. Create Agent Router
**File:** `api/routers/agent.py`
**Endpoints:**
- `POST /api/v1/agent/process-turn` - Process conversation turn
- `GET /api/v1/agent/sessions/{id}/context` - Get session context
- `POST /api/v1/agent/sessions/{id}/override` - Operator override

### 5. Fix Plugins Init
**File:** `plugins/__init__.py`
```python
from plugins.sarvam_stt import SarvamSTT
from plugins.sarvam_tts import SarvamTTS

__all__ = ["SarvamSTT", "SarvamTTS"]
```

---

## Technical Notes

### Configuration
All settings in `config.py` - uses Pydantic BaseSettings with `.env.local` file.

Key env vars needed:
```
LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
SARVAM_API_KEY
ANTHROPIC_API_KEY
DATABASE_URL
REDIS_URL
```

### Database
- PostgreSQL with asyncpg
- SQLAlchemy 2.0 async ORM
- Migrations via alembic (not yet set up)

### Redis
- Session storage: `agent:session:{call_session_id}` (4-hour TTL)
- Rate limiting counters
- Notification queue: `notifications:pending`

### Claude Integration
- Model: `claude-haiku-4-5`
- Temperature: 0.3
- Max tokens: 150
- Timeout: 3 seconds
- Tool use enabled for slot extraction and booking creation

---

## Code Patterns

### Adding a new API endpoint
```python
@router.post("/endpoint")
async def my_endpoint(
    body: MyRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    # ... logic ...
    return ResponseEnvelope(data=result, request_id=request_id)
```

### Redis session operations
```python
from agent_core.session import AgentSessionManager

session_manager = AgentSessionManager(redis)
session = await session_manager.get(call_session_id)
await session_manager.update(call_session_id, session)
```

### Tool execution
Tools are currently sync functions in TOOL_HANDLERS. For async DB operations, the state_machine may need to handle tool execution separately.

---

## Testing Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run FastAPI dev server
uvicorn main:app --reload --port 8000

# Run tests (when available)
pytest tests/
```

---

## Known Issues

1. `agent.py` is the LiveKit demo template, not the actual application. Don't use it.
2. `TOOL_HANDLERS` uses lambdas which don't support async - needs refactoring for DB operations
3. `caller_name` extraction is a placeholder in state_machine.py
4. Some agent endpoints in voice.spec.md don't exist yet (STT transcribe, TTS synthesize)

---

## Questions for Next Session

1. Should we implement streaming STT (currently uses one-shot)?
2. Do we need a separate worker process for LiveKit agent, or integrate with FastAPI?
3. What's the deployment topology - single container or split API/worker?
4. Should we add alembic migrations now or later?

---

## Working Directory

`/home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent/`

All Python code is inside the nested `vehicle-service-voice-agent` directory.

uvicorn main:app --reload --port 8000
python simple_agent.py dev
python -m http.server 3000  
lk dispatch create --new-room --agent-name speedcare-test-agent
lk dispatch create  --room room-EWpnBoRVkrp8  --agent-name speedcare-test-agen