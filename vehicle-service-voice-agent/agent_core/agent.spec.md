# agent.spec.md — Conversational Agent Module

---

## Feature: Conversational AI Agent & State Machine

### Goal
Implement the multi-stage conversational agent that receives transcripts from the voice pipeline, maintains conversation context across turns, classifies caller intent, collects required booking slots via Claude Haiku, executes tool calls against backend APIs, and produces natural language responses — operating as a deterministic state machine with measurable success conditions at each stage.

---

## Requirements

- The agent SHALL be implemented as a 3-stage state machine: `GreetingAgent → BookingAgent → ConfirmationAgent`.
- The agent SHALL use Claude Haiku-4-5 (`claude-haiku-4-5`) as the LLM with tool use enabled.
- The agent SHALL maintain a per-call conversation history object (list of `{role, content}` messages) in Redis, keyed by `call_session_id`.
- The system prompt SHALL be injected at the start of every LLM call; it SHALL NOT be stored in conversation history.
- The agent SHALL classify intent on every `BookingAgent` turn: `booking_new | booking_status | service_inquiry | out_of_scope`.
- For `booking_new`: agent SHALL collect 4 required slots: `vehicle_number`, `service_type`, `preferred_date`, `caller_name`.
- For `booking_status`: agent SHALL collect 1 slot: `booking_ref` OR `vehicle_number`.
- For `out_of_scope`: agent SHALL politely decline and re-steer to supported intents (max 1 deflection).
- Intent classification confidence below threshold SHALL trigger a clarification request (max 2 per call).
- The agent SHALL respond exclusively in the caller's detected language.
- The agent SHALL never fabricate booking references, prices, or availability — all factual data comes from tool calls.
- LLM tool calls SHALL have a 3-second timeout; on timeout, the agent SHALL respond with a graceful holding message and retry once.
- The entire LLM call (prompt + first token) SHALL complete within 1,200ms at P95.

---

## API Contract

### `POST /api/v1/agent/process-turn`
Process a single conversation turn. Called internally by the agent worker after receiving a final STT transcript.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request**
```json
{
  "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
  "turn_number": 3,
  "transcript": "TN 09 AK 1234 car brake problem uh service pannanum",
  "language": "ta",
  "agent_state": "collecting",
  "collected_slots": {
    "vehicle_number": "TN09AK1234",
    "service_type": null,
    "preferred_date": null,
    "caller_name": null
  }
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "response_text": "சரி, TN09AK1234 என்ற vehicle க்கு brake service வேண்டும் என்று புரிகிறது. எந்த தேதியில் service விரும்புகிறீர்கள்?",
    "next_agent_state": "collecting",
    "intent": "booking_new",
    "updated_slots": {
      "vehicle_number": "TN09AK1234",
      "service_type": "brake_service",
      "preferred_date": null,
      "caller_name": null
    },
    "tool_calls_made": ["normalize_vehicle_number"],
    "slots_remaining": ["preferred_date", "caller_name"],
    "action": "collect_slot",
    "llm_latency_ms": 720
  }
}
```

---

### `GET /api/v1/agent/sessions/{call_session_id}/context`
Retrieve the full conversation context for a call. Used by dashboard and debugging.

**Response 200**
```json
{
  "success": true,
  "data": {
    "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
    "agent_state": "confirming",
    "intent": "booking_new",
    "language": "ta",
    "turn_count": 6,
    "collected_slots": {
      "vehicle_number": "TN09AK1234",
      "service_type": "brake_service",
      "preferred_date": "2026-03-28",
      "caller_name": "Suresh"
    },
    "conversation_history": [
      { "role": "assistant", "content": "வணக்கம்! SpeedCare-ல் உங்களை வரவேற்கிறோம்..." },
      { "role": "user", "content": "TN 09 AK 1234 car brake problem..." }
    ],
    "retry_counts": {
      "clarification": 0,
      "llm_retry": 0
    }
  }
}
```

---

### `POST /api/v1/agent/sessions/{call_session_id}/override`
Operator override: manually advance or reset agent state. Used by dashboard supervisors.

**Request Headers**: `Authorization: Bearer <operator_token>`

**Request**
```json
{
  "new_state": "confirming",
  "reason": "operator_intervention",
  "force_slots": {
    "caller_name": "Suresh Kumar"
  }
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "previous_state": "collecting",
    "new_state": "confirming",
    "updated_at": "2026-03-25T10:04:00Z"
  }
}
```

---

## Data Model

### `agent_sessions` (Redis — per-call, TTL 4 hours)
```json
Key: "agent:session:{call_session_id}"
Value:
{
  "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
  "agent_state": "collecting",
  "intent": "booking_new",
  "language": "ta",
  "turn_count": 4,
  "clarification_retries": 0,
  "out_of_scope_count": 0,
  "silence_count": 0,
  "collected_slots": {
    "vehicle_number": "TN09AK1234",
    "service_type": "brake_service",
    "preferred_date": null,
    "caller_name": null
  },
  "conversation_history": [
    { "role": "assistant", "content": "..." },
    { "role": "user", "content": "..." }
  ],
  "last_updated": "2026-03-25T10:03:45Z"
}
```

### `agent_turns` (PostgreSQL — permanent record)
```sql
CREATE TABLE agent_turns (
    id                  BIGSERIAL PRIMARY KEY,
    call_session_id     UUID NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
    turn_number         INT NOT NULL,
    transcript          TEXT NOT NULL,
    intent_classified   VARCHAR(50),
    confidence          FLOAT,
    agent_response      TEXT NOT NULL,
    agent_state_before  VARCHAR(30),
    agent_state_after   VARCHAR(30),
    slots_before        JSONB,
    slots_after         JSONB,
    tool_calls          JSONB DEFAULT '[]'::jsonb,
    llm_model           VARCHAR(50) DEFAULT 'claude-haiku-4-5',
    llm_input_tokens    INT,
    llm_output_tokens   INT,
    llm_latency_ms      INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_agent_turns_session ON agent_turns(call_session_id, turn_number);
```

---

## Business Logic

### Agent State Machine

```
        ┌──────────────┐
 START → │ GreetingAgent│
        └──────┬───────┘
               │ greet + detect intent
               ▼
        ┌──────────────┐
        │ BookingAgent │ ◄──── re-entry on retry
        └──────┬───────┘
               │ all slots collected
               ▼
        ┌──────────────────┐
        │ ConfirmationAgent│
        └──────┬───────────┘
               │ caller confirms / cancels
               ▼
           CLOSING
```

**State Transitions**

| From | Trigger | To |
|---|---|---|
| `greeting` | Intent detected, slots incomplete | `collecting` |
| `greeting` | Intent = `booking_status` | `collecting` (status path) |
| `collecting` | All required slots filled | `confirming` |
| `collecting` | Caller says "cancel" | `closing` |
| `confirming` | Caller confirms | `closing` (booking API called) |
| `confirming` | Caller says "change" or "no" | `collecting` |
| Any | 3rd consecutive unclear input | `closing` (fallback) |
| Any | Silence timeout | `closing` |

---

### GreetingAgent

**System Prompt (injected, never in history)**
```
You are SpeedCare's voice assistant for vehicle service bookings.
Language: respond ONLY in {language}. Keep responses under 40 words.
Your ONLY tasks: 1) Greet caller. 2) Identify their intent.
Supported intents: book_new_service, check_booking_status, general_service_inquiry.
Do NOT discuss anything outside vehicle service.
```

**Logic**
1. Play pre-synthesized greeting TTS (zero LLM latency for first word).
2. Listen to first utterance; feed transcript to Claude with GreetingAgent system prompt.
3. Extract `intent` from LLM response (structured output via tool: `identify_intent`).
4. Transition to BookingAgent with detected intent.

---

### BookingAgent

**System Prompt**
```
You are collecting service booking information for a vehicle service center.
Language: respond ONLY in {language}. Keep responses under 50 words.
Current intent: {intent}.
Required slots: {slots_to_collect}.
Already collected: {collected_slots}.
Ask for ONE missing slot at a time. Be conversational and helpful.
Normalize vehicle numbers to format: XX00XX0000 (state code + digits + letters + digits).
If caller says something unrelated to vehicle service, politely redirect.
NEVER invent data. NEVER confirm a booking yourself — that happens next.
Available tools: normalize_vehicle_number, validate_date, check_service_type.
```

**Slot Collection Logic**
```python
REQUIRED_SLOTS = {
    "booking_new": ["vehicle_number", "service_type", "preferred_date", "caller_name"],
    "booking_status": ["booking_ref_or_vehicle"]
}

def next_slot_to_ask(collected_slots, intent):
    for slot in REQUIRED_SLOTS[intent]:
        if not collected_slots.get(slot):
            return slot
    return None  # all slots filled → transition to confirming
```

**Tool Definitions (Claude tool use)**
```json
[
  {
    "name": "normalize_vehicle_number",
    "description": "Normalize and validate an Indian vehicle registration number",
    "input_schema": {
      "type": "object",
      "properties": {
        "raw_input": { "type": "string" }
      },
      "required": ["raw_input"]
    }
  },
  {
    "name": "validate_date",
    "description": "Parse and validate a preferred appointment date. Returns ISO date or error.",
    "input_schema": {
      "type": "object",
      "properties": {
        "raw_date_string": { "type": "string" },
        "reference_date": { "type": "string", "description": "Today's date ISO8601" }
      },
      "required": ["raw_date_string", "reference_date"]
    }
  },
  {
    "name": "check_service_type",
    "description": "Map a natural language service description to a canonical service type code.",
    "input_schema": {
      "type": "object",
      "properties": {
        "description": { "type": "string" }
      },
      "required": ["description"]
    }
  },
  {
    "name": "lookup_booking_status",
    "description": "Look up an existing booking by reference number or vehicle number.",
    "input_schema": {
      "type": "object",
      "properties": {
        "booking_ref": { "type": "string" },
        "vehicle_number": { "type": "string" }
      }
    }
  }
]
```

---

### ConfirmationAgent

**System Prompt**
```
You are summarizing and confirming a service booking.
Language: respond ONLY in {language}. Keep response under 60 words.
Collected details: {collected_slots}.
Read back ALL details clearly: vehicle number, service type, date.
Then ask: "Shall I confirm this booking? Yes or No."
If caller says yes → use tool: create_booking.
If caller says no or asks to change → signal: return_to_collecting.
```

**Tool**
```json
{
  "name": "create_booking",
  "description": "Create a confirmed service booking and return a reference number.",
  "input_schema": {
    "type": "object",
    "properties": {
      "vehicle_number": { "type": "string" },
      "service_type": { "type": "string" },
      "preferred_date": { "type": "string", "format": "date" },
      "caller_name": { "type": "string" },
      "caller_number": { "type": "string" }
    },
    "required": ["vehicle_number", "service_type", "preferred_date", "caller_name"]
  }
}
```

**On successful `create_booking` tool response:**
1. Read back confirmation: `"Booking confirmed! Reference: SC-2026-0042. You'll receive an SMS shortly."`
2. Transition to `closing`.

---

### Clarification & Fallback Logic

```
clarification_retries per call: max 2
on unclear_intent or low_confidence:
  if clarification_retries < 2:
    clarification_retries += 1
    response = "மன்னிக்கவும், சரியாக புரியவில்லை. மீண்டும் சொல்ல முடியுமா?"
  else:
    play fallback_audio[language]
    log outcome=failed_clarification
    end_call()
```

---

### Conversation History Management

- Max history length: 20 turns (10 exchanges). Older turns pruned (keep system context, last 20).
- After each turn, updated history written back to Redis with TTL refresh.
- On call end: full history written to `call_sessions.transcript_url` as JSON in object storage (S3/MinIO).
- History key in Redis: `agent:session:{call_session_id}`, TTL: 4 hours.

---

## Edge Cases

| Scenario | Handling |
|---|---|
| Claude returns malformed tool call JSON | Parse defensively; if unrecoverable, use clarification retry |
| Tool call timeout (3s) | Respond with "one moment please" TTS; retry tool call once; if second timeout → use fallback |
| Caller provides all slots in one utterance | Extract all slots in single turn; skip individual questions; proceed to confirmation |
| Caller provides partial vehicle number (e.g. "TN09 car") | Ask specifically for full registration number |
| Date parsing: "next Monday", "day after tomorrow" | Resolve relative to today's date injected in system prompt |
| Intent switches mid-conversation (booking → status) | Detect mid-conversation intent change; reset slots; restart BookingAgent |
| LLM responds in wrong language | Detect language mismatch; re-inject language instruction; retry once |
| LLM context window overflow (>32K tokens) | Prune oldest non-system turns; keep last 10 turns + system prompt |
| Caller says "transfer to human" | Respond: "I'll connect you to our team. Please hold."; trigger notification to ops; end call with `outcome=transferred` |

---

## Constraints

- LLM model: `claude-haiku-4-5` exclusively (fastest for real-time voice).
- Max input tokens per turn: 4,000 (including system prompt + history + transcript).
- Max output tokens: 150 (voice responses must be concise).
- Temperature: 0.3 (deterministic slot extraction; minimal hallucination).
- Redis session TTL: 4 hours from last activity.
- Conversation history persisted to PostgreSQL on call end, not during call (to minimize write latency).
- Tool execution runs synchronously within the agent turn (not fire-and-forget) — results feed back to LLM.
- Claude API calls use a dedicated `httpx.AsyncClient` connection pool (max 30 connections).

---

## Acceptance Criteria

- [ ] GreetingAgent correctly identifies `booking_new` intent from 5 test utterances in Tamil/Hindi/English.
- [ ] BookingAgent collects all 4 slots in ≤ 5 turns on the happy path (test transcript provided).
- [ ] Slot `vehicle_number` normalized correctly for formats: `TN 09 AK 1234`, `tn09ak1234`, `TN-09-AK-1234`.
- [ ] Date `"day after tomorrow"` resolves to correct ISO date relative to injected reference date.
- [ ] `out_of_scope` intent returns deflection response and re-asks about service intent.
- [ ] After 2 clarification retries, call terminates with `outcome=failed_clarification`.
- [ ] `create_booking` tool is called exactly once per booking confirmation; not on retry/cancel.
- [ ] Conversation history in Redis matches PostgreSQL `agent_turns` records at call end.
- [ ] Claude API timeout (mocked) triggers retry + "please wait" response without crashing agent.
- [ ] `llm_input_tokens` + `llm_output_tokens` recorded for 100% of turns in `agent_turns`.
