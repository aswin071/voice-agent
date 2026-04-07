# voice.spec.md — Voice Pipeline Module

---

## Feature: Real-Time Voice Pipeline

### Goal
Establish and manage the full real-time audio pipeline for each inbound call: WebRTC/SIP ingestion via LiveKit, voice activity detection, noise cancellation, streaming speech-to-text via Sarvam saaras:v3, streaming text-to-speech via Sarvam bulbul:v3, and clean audio playback to the caller — all with latency under 2 seconds time-to-first-audio (TTFA) at P95.

---

## Requirements

- The pipeline SHALL accept inbound audio from two sources: SIP trunk (via LiveKit SIP participant) and browser WebRTC (via LiveKit room join).
- Voice Activity Detection (VAD) SHALL use Silero VAD with endpointing at 70ms trailing silence.
- Noise cancellation SHALL be applied via the LiveKit Noise Cancellation plugin before STT.
- Turn detection SHALL use LiveKit Turn Detector plugin to correctly identify end-of-turn in multilingual speech.
- STT SHALL stream audio frames to Sarvam saaras:v3 and return interim + final transcripts.
- Language detection SHALL occur on first final transcript; detected language SHALL be locked for the call session.
- TTS SHALL stream from Sarvam bulbul:v3; audio chunks SHALL be piped to LiveKit as they arrive (do not buffer full response).
- The agent SHALL support barge-in: when VAD detects caller speech during TTS playback, playback SHALL stop within 200ms.
- Silence timeout: 8 seconds of silence → agent prompts; 20 seconds cumulative silence → graceful call end.
- STT misrecognition retry: if LLM signals `intent=unclear`, agent re-asks (max 2 retries) before fallback.
- All audio frames, transcripts, and TTS events SHALL be timestamped for latency telemetry.
- The pipeline SHALL emit per-turn metrics: `stt_latency_ms`, `llm_first_token_ms`, `tts_first_chunk_ms`, `total_ttfa_ms`.

---

## API Contract

### `POST /api/v1/voice/rooms`
Create or join a LiveKit room for an inbound call. Called by the SIP dispatch rule handler or browser client.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request**
```json
{
  "call_sid": "EX-abc123",
  "caller_number": "+919876543210",
  "source": "sip",
  "sip_call_id": "abc123@sip.exotel.com",
  "metadata": {
    "trunk_id": "exotel-trunk-01",
    "dialed_number": "+914422334455"
  }
}
```

**Response 201**
```json
{
  "success": true,
  "data": {
    "room_name": "SC_ROOM_EX-abc123",
    "livekit_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
    "ws_url": "wss://speedcare.livekit.cloud",
    "sip_uri": "sip:SC_ROOM_EX-abc123@sip.livekit.cloud"
  }
}
```

---

### `POST /api/v1/voice/rooms/{room_name}/end`
Programmatically end a call and close the LiveKit room.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request**
```json
{
  "reason": "booking_complete",
  "outcome": "completed"
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "room_name": "SC_ROOM_EX-abc123",
    "ended_at": "2026-03-25T10:05:32Z",
    "duration_seconds": 142
  }
}
```

---

### `GET /api/v1/voice/rooms/{room_name}/status`
Get live call status including pipeline health.

**Response 200**
```json
{
  "success": true,
  "data": {
    "room_name": "SC_ROOM_EX-abc123",
    "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
    "status": "active",
    "participants": 2,
    "agent_state": "collecting",
    "language": "ta",
    "turn_count": 4,
    "last_activity_at": "2026-03-25T10:04:10Z",
    "pipeline_health": {
      "stt": "ok",
      "tts": "ok",
      "llm": "ok"
    }
  }
}
```

---

### `POST /api/v1/voice/stt/transcribe`
One-shot transcription endpoint (non-streaming) used for testing and transcript verification.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request** (multipart/form-data)
```
audio_file: <binary WAV/PCM 16kHz mono>
language_hint: "ta"
call_sid: "EX-abc123"
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "transcript": "என் காரை சர்வீஸ் பண்ண வேணும்",
    "language_detected": "ta",
    "confidence": 0.94,
    "word_timestamps": [
      { "word": "என்", "start_ms": 0, "end_ms": 210 },
      { "word": "காரை", "start_ms": 220, "end_ms": 510 }
    ],
    "processing_time_ms": 380
  }
}
```

---

### `POST /api/v1/voice/tts/synthesize`
One-shot TTS synthesis for testing and pre-canned audio generation.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request**
```json
{
  "text": "உங்கள் booking confirm ஆகிவிட்டது.",
  "language": "ta",
  "voice_id": "meera",
  "speed": 1.0,
  "format": "wav"
}
```

**Response 200** (audio/wav binary stream)

---

### `GET /api/v1/voice/calls`
List call sessions with filtering. Requires `Authorization: Bearer <token>` with `admin` or `operator` role.

**Query params**: `?date_from=2026-03-01&date_to=2026-03-25&outcome=completed&page=1&page_size=20`

**Response 200**
```json
{
  "success": true,
  "data": {
    "total": 142,
    "page": 1,
    "page_size": 20,
    "items": [
      {
        "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
        "call_sid": "EX-abc123",
        "caller_number": "+91 XXXXX X3210",
        "language": "ta",
        "started_at": "2026-03-25T10:00:00Z",
        "duration_seconds": 142,
        "outcome": "completed",
        "intent": "booking_new",
        "booking_ref": "SC-2026-0042"
      }
    ]
  }
}
```

---

## Data Model

### `voice_turns`
Stores every agent-caller exchange for latency analysis and transcript reconstruction.

```sql
CREATE TABLE voice_turns (
    id                  BIGSERIAL PRIMARY KEY,
    call_session_id     UUID NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
    turn_number         INT NOT NULL,
    direction           VARCHAR(10) NOT NULL,  -- 'caller' | 'agent'
    raw_audio_start_ms  BIGINT,               -- epoch ms
    vad_end_ms          BIGINT,
    stt_start_ms        BIGINT,
    stt_final_ms        BIGINT,
    llm_start_ms        BIGINT,
    llm_first_token_ms  BIGINT,
    tts_start_ms        BIGINT,
    tts_first_chunk_ms  BIGINT,
    audio_delivered_ms  BIGINT,
    transcript          TEXT,
    agent_response      TEXT,
    stt_confidence      FLOAT,
    language            VARCHAR(10),
    barge_in_occurred   BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_voice_turns_session ON voice_turns(call_session_id, turn_number);
```

### `stt_errors`
```sql
CREATE TABLE stt_errors (
    id              BIGSERIAL PRIMARY KEY,
    call_session_id UUID NOT NULL REFERENCES call_sessions(id),
    turn_number     INT,
    error_type      VARCHAR(50),  -- 'low_confidence' | 'empty_transcript' | 'timeout' | 'api_error'
    raw_response    JSONB,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## Business Logic

### Call Ingestion Flow

1. SIP provider delivers inbound call to LiveKit SIP trunk endpoint.
2. LiveKit emits `room_started` webhook to `POST /api/v1/voice/rooms`.
3. FastAPI handler creates `call_sessions` record with `status=active`.
4. Agent worker receives LiveKit `RoomEvent.ParticipantConnected`.
5. Worker initializes:
   - `NoiseCancellationPlugin`
   - `SileroVAD` (endpointing: 70ms trailing silence)
   - `TurnDetector`
   - `SarvamSTT` streaming session (language_hint from caller metadata or `auto`)
   - `SarvamTTS` client (voice selected per detected language)
6. Worker plays greeting TTS immediately (pre-canned audio, zero LLM latency for first turn).

### Per-Turn Pipeline

```
1. VAD detects speech start → set turn start timestamp
2. Audio frames streamed to Sarvam STT (16kHz, mono, PCM16)
3. VAD detects end-of-utterance (70ms trailing silence)
4. Sarvam STT returns final transcript + confidence
5. If confidence < 0.65:
   a. Increment low_confidence_count
   b. If low_confidence_count >= 2 → route to fallback
   c. Else → request clarification via TTS
6. Transcript + conversation history → Claude Haiku (via agent.spec.md)
7. LLM response text → Sarvam TTS streaming synthesis
8. First audio chunk arrives → stream to LiveKit track
9. Monitor VAD during TTS playback for barge-in
10. Log all timestamps to voice_turns
```

### Barge-In Handling

1. While TTS audio is playing, VAD runs on incoming audio channel.
2. If VAD detects speech with probability > 0.85:
   a. Cancel ongoing TTS audio stream.
   b. Send `flush` signal to LiveKit audio track.
   c. Start new STT session for new utterance.
   d. Do NOT log partial TTS as a complete agent turn.

### Silence Timeout

```
silence_counter = 0
per_turn:
  if no speech detected for 8s:
    silence_counter += 1
    agent plays: "क्या आप अभी भी हैं? / Are you still there?"
  if silence_counter >= 2 OR total_silence > 20s:
    agent plays: "कोई response न मिलने के कारण call समाप्त हो रही है।"
    trigger POST /voice/rooms/{room}/end with reason=silence_timeout
```

### Language Detection & Locking

1. First final STT transcript used for language detection (Sarvam returns `detected_language`).
2. Detected language stored in `call_sessions.language` and Redis session key.
3. All subsequent TTS responses use that language.
4. If caller switches language mid-call (detected on 2 consecutive turns): update session language, re-load appropriate TTS voice.

### Pre-Canned Fallback Audio

Pre-synthesize and cache 5 fallback audio files (WAV) at startup:
- `fallback_sorry_ta.wav`, `_hi.wav`, `_en.wav`, `_ml.wav`, `_mixed.wav`
- Used when STT or LLM fails after all retries; played immediately without latency.

---

## Edge Cases

| Scenario | Handling |
|---|---|
| STT API returns HTTP 503 | Retry up to 3 times with 200ms backoff; on final failure play fallback audio |
| STT returns confidence < 0.65 | Re-ask once ("Sorry, I didn't catch that, could you repeat?"); second low-confidence → fallback |
| TTS API timeout > 1,000ms | Log; play pre-canned "please wait" audio; retry TTS once; if second fail → read response as text via emergency TTS engine (gTTS locally) |
| Caller hangs up during TTS | LiveKit `ParticipantDisconnected` event → cancel TTS stream → write partial session to DB |
| STT returns empty string | Treat as silence event; apply silence counter logic |
| Audio glitch: PCM corrupted packet | VAD ignores sub-100ms corrupted frames; log anomaly counter |
| Concurrent barge-in + silence timeout | Barge-in takes priority; reset silence counter |
| Network jitter causing audio gaps | LiveKit jitter buffer (80ms) absorbs; STT handles discontinuous audio gracefully |

---

## Constraints

- Audio format: PCM 16-bit, 16kHz sample rate, mono channel, little-endian.
- Max audio frame size sent to STT: 3 seconds (48,000 bytes).
- STT streaming session timeout: 300 seconds (5 minutes max call length for streaming session).
- TTS character limit per synthesis request: 500 characters. Longer responses are chunked at sentence boundaries.
- LiveKit room TTL: 2 hours; room auto-closed by LiveKit after that.
- Max concurrent rooms per worker: 20.
- Sarvam API rate limit: 100 concurrent STT streams; agent workers share this via Redis counter.
- All audio files cached on disk in `/tmp/speedcare_audio/` with 24-hour TTL.

---

## Acceptance Criteria

- [ ] VAD correctly detects end-of-utterance within 70ms of trailing silence in audio test files.
- [ ] STT returns final transcript for a 5-second Tamil speech sample with confidence > 0.85.
- [ ] TTS begins emitting audio chunks within 300ms of receiving text input.
- [ ] Barge-in test: playing TTS + injecting new caller audio stops TTS playback within 200ms.
- [ ] End-to-end TTFA test: 10 consecutive turns average < 1,800ms, P95 < 2,000ms.
- [ ] Language detection correctly identifies Tamil, Hindi, English on first transcript (test set WER < 15%).
- [ ] Silence timeout: 20 seconds of injected silence triggers graceful call end with TTS farewell.
- [ ] STT failure (mocked 503) triggers retry; third failure plays fallback audio without crashing.
- [ ] All turns logged to `voice_turns` with complete timestamp fields and non-null `stt_final_ms`.
- [ ] `GET /voice/calls` returns masked caller numbers (no full phone number in response body).
