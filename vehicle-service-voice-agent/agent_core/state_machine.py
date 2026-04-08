from __future__ import annotations

import json
import logging
import re
import time
from datetime import date

import httpx

from agent_core.prompts import (
    BOOKING_SYSTEM_DYNAMIC, BOOKING_SYSTEM_PROMPT, BOOKING_SYSTEM_STATIC,
    CLARIFICATION_MESSAGES,
    CONFIRMATION_SYSTEM_DYNAMIC, CONFIRMATION_SYSTEM_PROMPT, CONFIRMATION_SYSTEM_STATIC,
    GREETING_SYSTEM_DYNAMIC, GREETING_SYSTEM_PROMPT, GREETING_SYSTEM_STATIC,
)
from agent_core.tools import (
    AGENT_TOOLS, TOOL_HANDLERS, async_create_booking, async_lookup_booking_status,
    check_service_type, normalize_vehicle_number, validate_date,
)
from config import get_settings

logger = logging.getLogger("speedcare.agent")
settings = get_settings()

LANGUAGE_NAMES = {
    "en": "English",
    "ta": "Tamil",
    "hi": "Hindi",
    "ml": "Malayalam",
}

REQUIRED_SLOTS = {
    "booking_new": ["vehicle_number", "service_type", "preferred_date", "caller_name"],
    "booking_status": ["booking_ref_or_vehicle"],
}

# Simple regex patterns for name extraction from common phrases
NAME_PATTERNS = [
    r"my name is ([\w\s]+)",
    r"i am ([\w\s]+)",
    r"name[\s:]+([\w\s]+)",
    r"call me ([\w\s]+)",
    r"this is ([\w\s]+)",
    r"(\w+) here",  # "Suresh here"
]


# Match Indian vehicle plates with optional spaces/dashes between groups, case-insensitive.
# Examples it catches: "TN09AK1234", "TN 09 AK 1234", "tn-09-ak-1234"
VEHICLE_PLATE_RE = re.compile(
    r"\b([A-Za-z]{2})[\s\-]*([0-9]{1,2})[\s\-]*([A-Za-z]{1,3})[\s\-]*([0-9]{4})\b"
)

# Date keywords / phrases. ORDER MATTERS — longer phrases first so that
# "day after tomorrow" doesn't get matched as "tomorrow".
DATE_KEYWORDS = [
    "day after tomorrow",
    "next monday", "next tuesday", "next wednesday", "next thursday",
    "next friday", "next saturday", "next sunday",
    "tomorrow", "today",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "நாளை மறுநாள்", "நாளை", "कल", "परसों", "നാളെ",
]

# Service-type keywords ordered specific-first so "AC service" doesn't fall
# into the generic "service" → general_service bucket.
SERVICE_KEYWORDS = [
    ("ac service", "ac_service"),
    ("air condition", "ac_service"),
    ("oil change", "oil_change"),
    ("engine oil", "oil_change"),
    ("brake service", "brake_service"),
    ("brake", "brake_service"),
    ("tyre rotation", "tyre_rotation"),
    ("tire rotation", "tyre_rotation"),
    ("tyre", "tyre_rotation"),
    ("tire", "tyre_rotation"),
    ("battery check", "battery_check"),
    ("battery", "battery_check"),
    ("full inspection", "full_inspection"),
    ("body repair", "body_repair"),
    ("dent", "body_repair"),
    ("scratch", "body_repair"),
    ("paint", "body_repair"),
    ("oil", "oil_change"),
    ("ac", "ac_service"),
    ("general service", "general_service"),
    ("regular service", "general_service"),
    ("routine service", "general_service"),
    ("general", "general_service"),
]


def extract_slots_from_transcript(transcript: str, current_slots: dict) -> dict:
    """Run deterministic extractors on the raw transcript and return any new
    slot values found. We do NOT depend on the LLM emitting tool calls.

    This is the bug fix for: Claude was replying conversationally without
    invoking normalize_vehicle_number/check_service_type/validate_date, so
    slots never filled and the FSM stayed forever in 'collecting'.
    """
    found = {}

    # Vehicle number — scan transcript for a plate-shaped substring
    if not current_slots.get("vehicle_number"):
        m = VEHICLE_PLATE_RE.search(transcript)
        if m:
            raw = "".join(m.groups())
            result = normalize_vehicle_number(raw)
            if result.get("valid"):
                found["vehicle_number"] = result["normalized"]

    # Service type — specific-first keyword match. We use our own ordered
    # list rather than check_service_type() because that function iterates
    # an unordered dict and matches generic words first.
    if not current_slots.get("service_type"):
        lower_t = transcript.lower()
        for kw, code in SERVICE_KEYWORDS:
            if kw in lower_t:
                found["service_type"] = code
                break

    # Date — try date keywords, then ISO/numeric formats
    if not current_slots.get("preferred_date"):
        lower = transcript.lower()
        date_input = None
        for kw in DATE_KEYWORDS:
            if kw in lower:
                date_input = kw
                break
        if not date_input:
            # Try matching an ISO date or DD/MM/YYYY anywhere in text
            m = re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", transcript)
            if m:
                date_input = m.group()
        if date_input:
            result = validate_date(date_input)
            if result.get("valid"):
                found["preferred_date"] = result["date"]

    return found


def extract_caller_name(transcript: str) -> str | None:
    """Extract caller name from transcript using simple patterns."""
    import re
    lower = transcript.lower()

    for pattern in NAME_PATTERNS:
        match = re.search(pattern, lower)
        if match:
            name = match.group(1).strip()
            # Clean up common suffixes
            name = re.sub(r"\s+(and|i|my|from|speaking).*$", "", name, flags=re.I)
            return name.title() if name else None

    return None


class ConversationalAgent:
    """Processes a single turn through the agent state machine."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        db=None,  # AsyncSession for database operations
    ):
        self.client = http_client or httpx.AsyncClient(
            timeout=8.0,
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60.0),
        )
        self.db = db

    async def process_turn(
        self,
        transcript: str,
        session: dict,
    ) -> dict:
        state = session.get("agent_state", "greeting")
        language = LANGUAGE_NAMES.get(session.get("language", "ta"), "English")
        collected_slots = session.get("collected_slots", {})
        intent = session.get("intent")

        if state == "greeting":
            return await self._greeting_turn(transcript, session, language)
        elif state == "collecting":
            return await self._booking_turn(transcript, session, language, intent, collected_slots)
        elif state == "confirming":
            return await self._confirmation_turn(transcript, session, language, collected_slots)
        else:
            return {
                "response_text": "Thank you for calling SpeedCare. Goodbye!",
                "next_agent_state": "closing",
                "intent": intent,
                "updated_slots": collected_slots,
                "tool_calls_made": [],
                "slots_remaining": [],
                "action": "end_call",
                "llm_latency_ms": 0,
            }

    async def _greeting_turn(self, transcript: str, session: dict, language: str) -> dict:
        system_dynamic = GREETING_SYSTEM_DYNAMIC.format(
            language=language,
            today=date.today().isoformat(),
        )
        # Only use identify_intent tool for greeting
        tools = [t for t in AGENT_TOOLS if t["name"] == "identify_intent"]

        response, latency, tool_calls = await self._call_llm(
            "", session["conversation_history"], transcript, tools,
            system_static=GREETING_SYSTEM_STATIC,
            system_dynamic=system_dynamic,
        )

        intent = "booking_new"
        for tc in tool_calls:
            if tc["name"] == "identify_intent":
                intent = tc["result"].get("intent", "booking_new")

        next_state = "collecting"
        return {
            "response_text": response,
            "next_agent_state": next_state,
            "intent": intent,
            "updated_slots": session["collected_slots"],
            "tool_calls_made": [tc["name"] for tc in tool_calls],
            "slots_remaining": REQUIRED_SLOTS.get(intent, []),
            "action": "collect_slot",
            "llm_latency_ms": latency,
        }

    async def _booking_turn(self, transcript: str, session: dict, language: str, intent: str, collected_slots: dict) -> dict:
        # Deterministic slot extraction from the transcript itself.
        # We do this BEFORE calling the LLM so the prompt sees the freshly
        # filled slots and asks for the next missing one. We don't rely on
        # Claude calling tools — it often doesn't.
        deterministic = extract_slots_from_transcript(transcript, collected_slots)
        if deterministic:
            collected_slots = {**collected_slots, **deterministic}
            logger.info("deterministic_slot_fill", extra={"slots": deterministic})

        missing = [s for s in REQUIRED_SLOTS.get(intent, []) if not collected_slots.get(s)]

        system_dynamic = BOOKING_SYSTEM_DYNAMIC.format(
            language=language,
            intent=intent,
            slots_to_collect=json.dumps(missing),
            collected_slots=json.dumps(collected_slots),
            today=date.today().isoformat(),
        )

        # Tools tax — every tool we send is ~70-100 input tokens that Claude
        # has to process every turn. The deterministic slot extractor above
        # already handles vehicle_number / preferred_date / service_type, so
        # for booking_new turns we send ZERO tools. lookup_booking_status is
        # only meaningful for booking_status intent, so we gate it on intent.
        # This drops ~300 input tokens per turn → ~150-300 ms saved.
        if intent == "booking_status":
            tools = [t for t in AGENT_TOOLS if t["name"] == "lookup_booking_status"]
        else:
            tools = []

        response, latency, tool_calls = await self._call_llm(
            "", session["conversation_history"], transcript, tools,
            system_static=BOOKING_SYSTEM_STATIC,
            system_dynamic=system_dynamic,
        )

        # Process tool results to update slots
        updated_slots = dict(collected_slots)
        booking_status_result = None

        for tc in tool_calls:
            result = tc.get("result", {})
            if tc["name"] == "normalize_vehicle_number" and result.get("valid"):
                updated_slots["vehicle_number"] = result["normalized"]
            elif tc["name"] == "validate_date" and result.get("valid"):
                updated_slots["preferred_date"] = result["date"]
            elif tc["name"] == "check_service_type" and result.get("valid"):
                updated_slots["service_type"] = result["service_type"]
            elif tc["name"] == "lookup_booking_status" and self.db:
                # Handle async booking lookup
                try:
                    input_args = tc.get("input", {})
                    booking_status_result = await async_lookup_booking_status(
                        self.db,
                        booking_ref=input_args.get("booking_ref"),
                        vehicle_number=input_args.get("vehicle_number", updated_slots.get("vehicle_number")),
                    )
                    tc["result"] = booking_status_result
                    # If search by vehicle number, store the normalized number
                    if input_args.get("vehicle_number") and booking_status_result.get("valid"):
                        updated_slots["vehicle_number"] = booking_status_result.get("vehicle_number")
                except Exception as e:
                    logger.error("lookup_booking_status_error", extra={"error": str(e)})
                    tc["result"] = {"error": str(e)}

        # Try extracting caller_name from transcript if not yet collected
        if not updated_slots.get("caller_name"):
            extracted_name = extract_caller_name(transcript)
            if extracted_name:
                updated_slots["caller_name"] = extracted_name

        remaining = [s for s in REQUIRED_SLOTS.get(intent, []) if not updated_slots.get(s)]
        next_state = "confirming" if not remaining else "collecting"

        # For booking_status intent with result, transition to closing
        if intent == "booking_status" and booking_status_result:
            next_state = "closing"

        return {
            "response_text": response,
            "next_agent_state": next_state,
            "intent": intent,
            "updated_slots": updated_slots,
            "tool_calls_made": [tc["name"] for tc in tool_calls],
            "slots_remaining": remaining,
            "action": "confirm" if not remaining else "collect_slot",
            "llm_latency_ms": latency,
        }

    async def _confirmation_turn(self, transcript: str, session: dict, language: str, collected_slots: dict) -> dict:
        """Confirmation stage. We do NOT depend on Claude calling
        create_booking — we detect a yes/no in the transcript ourselves and
        invoke async_create_booking directly. This was the second half of
        the booking-not-saved bug: Claude often replies in text without
        emitting the tool call, so the booking never reached Postgres."""
        lower = transcript.lower().strip()

        affirm_words = (
            "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct",
            "confirm", "right", "perfect", "go ahead", "please do", "fine",
            "ஆம்", "சரி", "हाँ", "हां", "ठीक", "अच्छा", "അതെ", "ശരി",
        )
        deny_words = (
            "no", "nope", "wrong", "change", "modify", "cancel", "incorrect",
            "இல்லை", "மாற்ற", "नहीं", "बदल", "गलत", "അല്ല",
        )

        is_affirm = any(w in lower for w in affirm_words)
        is_deny = any(w in lower for w in deny_words)
        # "no" is a substring of many words; require it as a token if affirm wasn't matched
        if is_deny and not is_affirm:
            pass  # treat as deny
        elif is_affirm:
            is_deny = False

        # Path 1: caller said NO → bounce back to collecting so they can
        # correct a slot. Don't bother calling the LLM here at all.
        if is_deny:
            return {
                "response_text": "No problem. What would you like to change?",
                "next_agent_state": "collecting",
                "intent": session.get("intent"),
                "updated_slots": collected_slots,
                "tool_calls_made": [],
                "slots_remaining": list(collected_slots.keys()),
                "action": "return_to_collecting",
                "llm_latency_ms": 0,
            }

        # Path 2: caller said YES → persist the booking ourselves and
        # produce the confirmation reply deterministically. No LLM call.
        if is_affirm:
            if not self.db:
                logger.error("confirm_yes_but_no_db_session")
                return {
                    "response_text": "Sorry, I couldn't save the booking just now. Please try again.",
                    "next_agent_state": "closing",
                    "intent": session.get("intent"),
                    "updated_slots": collected_slots,
                    "tool_calls_made": [],
                    "slots_remaining": [],
                    "action": "booking_failed",
                    "llm_latency_ms": 0,
                }

            try:
                booking_result = await async_create_booking(
                    self.db,
                    vehicle_number=collected_slots.get("vehicle_number"),
                    service_type=collected_slots.get("service_type"),
                    preferred_date=collected_slots.get("preferred_date"),
                    caller_name=collected_slots.get("caller_name"),
                    caller_number=None,
                    call_session_id=str(session.get("call_session_id", "")),
                )
            except Exception as e:
                logger.exception("create_booking_failed")
                return {
                    "response_text": "Sorry, I had a problem saving your booking. Please call back shortly.",
                    "next_agent_state": "closing",
                    "intent": session.get("intent"),
                    "updated_slots": collected_slots,
                    "tool_calls_made": [],
                    "slots_remaining": [],
                    "action": "booking_failed",
                    "llm_latency_ms": 0,
                }

            if not booking_result.get("valid"):
                logger.error("create_booking_invalid", extra={"result": booking_result})
                return {
                    "response_text": f"Sorry, I couldn't book that: {booking_result.get('error', 'unknown error')}",
                    "next_agent_state": "closing",
                    "intent": session.get("intent"),
                    "updated_slots": collected_slots,
                    "tool_calls_made": [],
                    "slots_remaining": [],
                    "action": "booking_failed",
                    "llm_latency_ms": 0,
                }

            ref = booking_result["booking_ref"]
            slot = booking_result.get("appointment_slot", "")
            appt_date = booking_result.get("appointment_date", collected_slots.get("preferred_date"))
            reply = (
                f"Booked! Your reference is {ref}. "
                f"We'll see you on {appt_date} at {slot}. Thank you for choosing SpeedCare!"
            )
            logger.info("booking_persisted", extra={"booking_ref": ref})

            return {
                "response_text": reply,
                "next_agent_state": "closing",
                "intent": session.get("intent"),
                "updated_slots": collected_slots,
                "tool_calls_made": ["create_booking"],
                "slots_remaining": [],
                "action": "booking_confirmed",
                "llm_latency_ms": 0,
            }

        # Path 3: ambiguous transcript ("um", "what?") → ask the LLM for a
        # short re-confirmation prompt and stay in confirming state.
        system_dynamic = CONFIRMATION_SYSTEM_DYNAMIC.format(
            language=language,
            collected_slots=json.dumps(collected_slots),
            today=date.today().isoformat(),
        )
        response, latency, _ = await self._call_llm(
            "", session["conversation_history"], transcript, [],
            system_static=CONFIRMATION_SYSTEM_STATIC,
            system_dynamic=system_dynamic,
        )
        return {
            "response_text": response or "Could you please say yes or no?",
            "next_agent_state": "confirming",
            "intent": session.get("intent"),
            "updated_slots": collected_slots,
            "tool_calls_made": [],
            "slots_remaining": [],
            "action": "awaiting_confirmation",
            "llm_latency_ms": latency,
        }

    async def _call_llm(
        self,
        system_prompt: str,
        history: list[dict],
        current_transcript: str,
        tools: list[dict],
        system_static: str | None = None,
        system_dynamic: str | None = None,
    ) -> tuple[str, int, list[dict]]:
        """Call Claude Haiku once. Returns (response_text, latency_ms, tool_calls).

        Two ways to pass the system prompt:
          - legacy: single `system_prompt` string (no real prefix caching).
          - preferred: `system_static` + `system_dynamic`. The static block
            is byte-identical across turns and gets cache_control=ephemeral
            so Anthropic prefix-caches it (cache reads are ~10% the cost AND
            ~3-5x faster than fresh tokens). The dynamic block carries the
            per-turn state and is NOT cached.

        History window is small on purpose: we only keep the last 6 messages.
        Each extra historical turn adds prompt tokens that the model has to
        process, which directly maps to LLM latency. Slot-filling rarely
        needs more than 2-3 turns of context.
        """
        messages = []
        for h in history[-6:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": current_transcript})

        # Build system blocks. Prefer the split version when caller provided it.
        if system_static is not None and system_dynamic is not None:
            system_blocks = [
                {
                    "type": "text",
                    "text": system_static,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": system_dynamic,
                },
            ]
        else:
            system_blocks = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        start = time.monotonic()

        try:
            body: dict = {
                "model": settings.LLM_MODEL,
                "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
                "temperature": settings.LLM_TEMPERATURE,
                "system": system_blocks,
                "messages": messages,
            }
            if tools:
                body["tools"] = tools

            resp = await self.client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
                timeout=8.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            body_text = getattr(getattr(e, "response", None), "text", "")
            logger.error("llm_call_failed: %s | anthropic_body: %s", str(e), body_text)
            return CLARIFICATION_MESSAGES.get(
                "en", "Sorry, please try again."
            ), latency, []

        latency = int((time.monotonic() - start) * 1000)

        # Cache hit/miss telemetry — useful when measuring savings
        usage = data.get("usage", {})
        if usage:
            logger.info(
                "llm_usage",
                extra={
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read": usage.get("cache_read_input_tokens"),
                    "cache_write": usage.get("cache_creation_input_tokens"),
                    "latency_ms": latency,
                },
            )

        # Extract text and tool use blocks
        response_text = ""
        tool_calls = []

        for block in data.get("content", []):
            if block["type"] == "text":
                response_text += block["text"]
            elif block["type"] == "tool_use":
                tool_name = block["name"]
                tool_input = block["input"]

                # Execute tool locally (sync handlers only here; async DB
                # tools are handled in _booking_turn / _confirmation_turn)
                handler = TOOL_HANDLERS.get(tool_name)
                if handler:
                    result = handler(tool_input)
                else:
                    result = tool_input

                tool_calls.append({
                    "name": tool_name,
                    "input": tool_input,
                    "result": result,
                })

        # If the model only emitted tool_use (no text), don't round-trip a
        # second LLM call — synthesize a short deterministic acknowledgement
        # from the tool result. The next user turn will produce real text.
        if not response_text and tool_calls:
            response_text = self._template_reply_for_tools(tool_calls)

        return response_text, latency, tool_calls

    # ── Streaming path ──────────────────────────────────────────────────────
    # Real LLM→TTS overlap. The LiveKit framework consumes whatever the
    # Agent.llm_node generator yields, batches by sentence boundary, and
    # hands each completed sentence to TTS. So as soon as Claude has emitted
    # the first sentence, Sarvam starts synthesizing it while Claude is
    # still generating sentence two. Time-to-first-audio drops by ~half the
    # LLM gen time.

    async def _stream_llm(
        self,
        out: dict,
        *,
        system_static: str,
        system_dynamic: str,
        history: list[dict],
        current_transcript: str,
        tools: list[dict],
    ):
        """Stream Claude SSE response, yielding text deltas as they arrive.

        `out` is mutated in place with the final state once the stream ends:
            out["text"]        full assembled response text
            out["tool_calls"]  list of {name, input, result} dicts
            out["latency_ms"]  total wall-clock for the LLM call
        """
        out["text"] = ""
        out["tool_calls"] = []
        out["latency_ms"] = 0

        messages = [{"role": h["role"], "content": h["content"]} for h in history[-6:]]
        messages.append({"role": "user", "content": current_transcript})

        system_blocks = [
            {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system_dynamic},
        ]

        payload = {
            "model": settings.LLM_MODEL,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            "temperature": settings.LLM_TEMPERATURE,
            "system": system_blocks,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        start = time.monotonic()

        # Track in-progress tool_use block (Anthropic streams each tool's
        # JSON args as input_json_delta events; we accumulate then parse).
        current_tool: dict | None = None
        current_tool_json = ""

        try:
            async with self.client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.error(
                        "llm_stream_http_error",
                        extra={"status": resp.status_code, "body": body[:300]},
                    )
                    out["latency_ms"] = int((time.monotonic() - start) * 1000)
                    yield "Sorry, please say that again."
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw == "[DONE]":
                        break
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    etype = evt.get("type")
                    if etype == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            current_tool = {"name": block.get("name", ""), "input": {}, "result": {}}
                            current_tool_json = ""
                    elif etype == "content_block_delta":
                        delta = evt.get("delta", {})
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                out["text"] += text
                                yield text
                        elif dtype == "input_json_delta":
                            current_tool_json += delta.get("partial_json", "")
                    elif etype == "content_block_stop":
                        if current_tool is not None:
                            try:
                                current_tool["input"] = (
                                    json.loads(current_tool_json) if current_tool_json else {}
                                )
                            except json.JSONDecodeError:
                                current_tool["input"] = {}
                            handler = TOOL_HANDLERS.get(current_tool["name"])
                            if handler:
                                try:
                                    current_tool["result"] = handler(current_tool["input"])
                                except Exception as e:
                                    current_tool["result"] = {"error": str(e)}
                            out["tool_calls"].append(current_tool)
                            current_tool = None
                            current_tool_json = ""
                    elif etype == "message_delta":
                        usage = evt.get("usage", {})
                        if usage:
                            logger.info(
                                "llm_stream_usage",
                                extra={
                                    "output_tokens": usage.get("output_tokens"),
                                },
                            )
        except Exception as e:
            logger.exception("llm_stream_failed")
            out["latency_ms"] = int((time.monotonic() - start) * 1000)
            if not out["text"]:
                yield "Sorry, please say that again."
            return

        out["latency_ms"] = int((time.monotonic() - start) * 1000)

        # If the model emitted only tool_use (no text), synthesize a short
        # template reply so the caller hears *something*.
        if not out["text"] and out["tool_calls"]:
            tmpl = self._template_reply_for_tools(out["tool_calls"])
            out["text"] = tmpl
            yield tmpl

    async def process_turn_stream(self, transcript: str, session: dict):
        """Streaming dispatcher. Yields text chunks; mutates `session` in place
        with the new agent_state, slots, intent, history, and turn_count once
        the underlying generator finishes."""
        state = session.get("agent_state", "greeting")
        if state == "greeting":
            async for chunk in self._greeting_turn_stream(transcript, session):
                yield chunk
        elif state == "collecting":
            async for chunk in self._booking_turn_stream(transcript, session):
                yield chunk
        elif state == "confirming":
            async for chunk in self._confirmation_turn_stream(transcript, session):
                yield chunk
        else:
            text = "Thank you for calling SpeedCare. Goodbye!"
            session["agent_state"] = "closing"
            session["conversation_history"].append({"role": "user", "content": transcript})
            session["conversation_history"].append({"role": "assistant", "content": text})
            yield text

    async def _greeting_turn_stream(self, transcript: str, session: dict):
        language = LANGUAGE_NAMES.get(session.get("language", "en"), "English")
        system_dynamic = GREETING_SYSTEM_DYNAMIC.format(
            language=language,
            today=date.today().isoformat(),
        )
        # identify_intent is a sync tool the model can emit; deterministic
        # default is booking_new and the prompt is tiny, so this is fine.
        tools = [t for t in AGENT_TOOLS if t["name"] == "identify_intent"]

        out: dict = {}
        async for chunk in self._stream_llm(
            out,
            system_static=GREETING_SYSTEM_STATIC,
            system_dynamic=system_dynamic,
            history=session["conversation_history"],
            current_transcript=transcript,
            tools=tools,
        ):
            yield chunk

        intent = "booking_new"
        for tc in out["tool_calls"]:
            if tc["name"] == "identify_intent":
                intent = tc.get("result", {}).get("intent", "booking_new")

        # Mutate session
        session["intent"] = intent
        session["agent_state"] = "collecting"
        session["conversation_history"].append({"role": "user", "content": transcript})
        session["conversation_history"].append({"role": "assistant", "content": out["text"]})
        session["turn_count"] = session.get("turn_count", 0) + 1
        logger.info("greet_stream_done", extra={"intent": intent, "ms": out["latency_ms"]})

    async def _booking_turn_stream(self, transcript: str, session: dict):
        language = LANGUAGE_NAMES.get(session.get("language", "en"), "English")
        intent = session.get("intent") or "booking_new"
        collected_slots = dict(session.get("collected_slots", {}))

        # Deterministic slot extraction (vehicle/date/service) BEFORE LLM call.
        deterministic = extract_slots_from_transcript(transcript, collected_slots)
        if deterministic:
            collected_slots.update(deterministic)
            logger.info("deterministic_slot_fill", extra={"slots": deterministic})

        # Caller name from common phrases
        if not collected_slots.get("caller_name"):
            extracted_name = extract_caller_name(transcript)
            if extracted_name:
                collected_slots["caller_name"] = extracted_name

        missing = [s for s in REQUIRED_SLOTS.get(intent, []) if not collected_slots.get(s)]

        system_dynamic = BOOKING_SYSTEM_DYNAMIC.format(
            language=language,
            intent=intent,
            slots_to_collect=json.dumps(missing),
            collected_slots=json.dumps(collected_slots),
            today=date.today().isoformat(),
        )

        # No tools for booking_new — see _booking_turn for the rationale.
        if intent == "booking_status":
            tools = [t for t in AGENT_TOOLS if t["name"] == "lookup_booking_status"]
        else:
            tools = []

        out: dict = {}
        async for chunk in self._stream_llm(
            out,
            system_static=BOOKING_SYSTEM_STATIC,
            system_dynamic=system_dynamic,
            history=session["conversation_history"],
            current_transcript=transcript,
            tools=tools,
        ):
            yield chunk

        # lookup_booking_status is the only async tool that may need DB access
        for tc in out["tool_calls"]:
            if tc["name"] == "lookup_booking_status" and self.db:
                try:
                    input_args = tc.get("input", {})
                    res = await async_lookup_booking_status(
                        self.db,
                        booking_ref=input_args.get("booking_ref"),
                        vehicle_number=input_args.get(
                            "vehicle_number", collected_slots.get("vehicle_number")
                        ),
                    )
                    tc["result"] = res
                    if input_args.get("vehicle_number") and res.get("valid"):
                        collected_slots["vehicle_number"] = res.get("vehicle_number")
                except Exception as e:
                    logger.error("lookup_booking_status_error", extra={"error": str(e)})

        remaining = [s for s in REQUIRED_SLOTS.get(intent, []) if not collected_slots.get(s)]
        next_state = "confirming" if not remaining else "collecting"

        # Mutate session
        session["collected_slots"] = collected_slots
        session["agent_state"] = next_state
        session["conversation_history"].append({"role": "user", "content": transcript})
        session["conversation_history"].append({"role": "assistant", "content": out["text"]})
        session["turn_count"] = session.get("turn_count", 0) + 1
        logger.info(
            "booking_stream_done",
            extra={
                "next_state": next_state,
                "remaining": remaining,
                "ms": out["latency_ms"],
            },
        )

    async def _confirmation_turn_stream(self, transcript: str, session: dict):
        """Three paths: deny / affirm / ambiguous. Only ambiguous calls the LLM
        — the other two yield a single deterministic chunk and skip Claude."""
        language = LANGUAGE_NAMES.get(session.get("language", "en"), "English")
        collected_slots = dict(session.get("collected_slots", {}))

        lower = transcript.lower().strip()
        affirm_words = (
            "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct",
            "confirm", "right", "perfect", "go ahead", "please do", "fine",
            "ஆம்", "சரி", "हाँ", "हां", "ठीक", "अच्छा", "അതെ", "ശരി",
        )
        deny_words = (
            "no", "nope", "wrong", "change", "modify", "cancel", "incorrect",
            "இல்லை", "மாற்ற", "नहीं", "बदल", "गलत", "അല്ല",
        )
        is_affirm = any(w in lower for w in affirm_words)
        is_deny = any(w in lower for w in deny_words)
        if is_deny and not is_affirm:
            pass
        elif is_affirm:
            is_deny = False

        # Path 1 — deny: bounce to collecting, no LLM, no TTS streaming
        if is_deny:
            text = "No problem. What would you like to change?"
            session["agent_state"] = "collecting"
            session["conversation_history"].append({"role": "user", "content": transcript})
            session["conversation_history"].append({"role": "assistant", "content": text})
            yield text
            return

        # Path 2 — affirm: persist booking, deterministic reply, no LLM
        if is_affirm:
            if not self.db:
                text = "Sorry, I couldn't save the booking just now. Please try again."
                session["agent_state"] = "closing"
                yield text
                return
            try:
                booking_result = await async_create_booking(
                    self.db,
                    vehicle_number=collected_slots.get("vehicle_number"),
                    service_type=collected_slots.get("service_type"),
                    preferred_date=collected_slots.get("preferred_date"),
                    caller_name=collected_slots.get("caller_name"),
                    caller_number=None,
                    call_session_id=str(session.get("call_session_id", "")),
                )
            except Exception:
                logger.exception("create_booking_failed_stream")
                text = "Sorry, I had a problem saving your booking. Please call back shortly."
                session["agent_state"] = "closing"
                yield text
                return

            if not booking_result.get("valid"):
                logger.error("create_booking_invalid_stream", extra={"result": booking_result})
                text = f"Sorry, I couldn't book that: {booking_result.get('error', 'unknown error')}"
                session["agent_state"] = "closing"
                yield text
                return

            ref = booking_result["booking_ref"]
            slot = booking_result.get("appointment_slot", "")
            appt_date = booking_result.get("appointment_date", collected_slots.get("preferred_date"))
            text = (
                f"Booked! Your reference is {ref}. "
                f"We'll see you on {appt_date} at {slot}. Thank you for choosing SpeedCare!"
            )
            session["agent_state"] = "closing"
            session["conversation_history"].append({"role": "user", "content": transcript})
            session["conversation_history"].append({"role": "assistant", "content": text})
            logger.info("booking_persisted_stream", extra={"booking_ref": ref})
            yield text
            return

        # Path 3 — ambiguous: stream the LLM for a re-confirmation prompt
        system_dynamic = CONFIRMATION_SYSTEM_DYNAMIC.format(
            language=language,
            collected_slots=json.dumps(collected_slots),
            today=date.today().isoformat(),
        )
        out: dict = {}
        async for chunk in self._stream_llm(
            out,
            system_static=CONFIRMATION_SYSTEM_STATIC,
            system_dynamic=system_dynamic,
            history=session["conversation_history"],
            current_transcript=transcript,
            tools=[],
        ):
            yield chunk

        session["conversation_history"].append({"role": "user", "content": transcript})
        session["conversation_history"].append({"role": "assistant", "content": out["text"]})

    @staticmethod
    def _template_reply_for_tools(tool_calls: list[dict]) -> str:
        """Build a one-line ack from a tool result so we can skip the
        second LLM round trip. Keep it short and conversational."""
        for tc in tool_calls:
            name = tc["name"]
            result = tc.get("result", {}) or {}
            if name == "normalize_vehicle_number" and result.get("valid"):
                return f"Got it, vehicle {result['normalized']}. What service do you need?"
            if name == "validate_date" and result.get("valid"):
                return f"Booked for {result['date']}. And your name, please?"
            if name == "check_service_type" and result.get("valid"):
                label = result.get("service_label") or result.get("service_type")
                return f"{label} — got it. What date works for you?"
            if name == "identify_intent":
                return "Sure, I can help with that. What is your vehicle number?"
        return "Got it. Could you tell me the next detail?"
