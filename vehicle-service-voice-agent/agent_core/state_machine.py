from __future__ import annotations

import json
import logging
import re
import time
from datetime import date

import httpx

from agent_core.prompts import (
    BOOKING_SYSTEM_PROMPT, CLARIFICATION_MESSAGES, CONFIRMATION_SYSTEM_PROMPT,
    GREETING_SYSTEM_PROMPT,
)
from agent_core.tools import (
    AGENT_TOOLS, TOOL_HANDLERS, async_create_booking, async_lookup_booking_status,
    check_service_type, normalize_vehicle_number, validate_date,
)
from config import get_settings

logger = logging.getLogger("speedcare.agent")
settings = get_settings()

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

# Date keywords / phrases we'll try to extract from the raw transcript
DATE_KEYWORDS = [
    "today", "tomorrow", "day after tomorrow",
    "next monday", "next tuesday", "next wednesday", "next thursday",
    "next friday", "next saturday", "next sunday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "நாளை", "कल", "नाळे", "परसों",
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

    # Service type — keyword match against transcript
    if not current_slots.get("service_type"):
        result = check_service_type(transcript)
        if result.get("valid"):
            found["service_type"] = result["service_type"]

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
        self.client = http_client or httpx.AsyncClient(timeout=5)
        self.db = db

    async def process_turn(
        self,
        transcript: str,
        session: dict,
    ) -> dict:
        state = session.get("agent_state", "greeting")
        language = session.get("language", "ta")
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
        system_prompt = GREETING_SYSTEM_PROMPT.format(
            language=language,
            today=date.today().isoformat(),
        )
        # Only use identify_intent tool for greeting
        tools = [t for t in AGENT_TOOLS if t["name"] == "identify_intent"]

        response, latency, tool_calls = await self._call_llm(
            system_prompt, session["conversation_history"], transcript, tools
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

        system_prompt = BOOKING_SYSTEM_PROMPT.format(
            language=language,
            intent=intent,
            slots_to_collect=json.dumps(missing),
            collected_slots=json.dumps(collected_slots),
            today=date.today().isoformat(),
        )

        tools = [t for t in AGENT_TOOLS if t["name"] in (
            "normalize_vehicle_number", "validate_date", "check_service_type", "lookup_booking_status"
        )]

        response, latency, tool_calls = await self._call_llm(
            system_prompt, session["conversation_history"], transcript, tools
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
        system_prompt = CONFIRMATION_SYSTEM_PROMPT.format(
            language=language,
            collected_slots=json.dumps(collected_slots),
            today=date.today().isoformat(),
        )

        tools = [t for t in AGENT_TOOLS if t["name"] == "create_booking"]

        response, latency, tool_calls = await self._call_llm(
            system_prompt, session["conversation_history"], transcript, tools
        )

        # Handle create_booking tool call with DB access
        booking_created = False
        booking_result = None

        for tc in tool_calls:
            if tc["name"] == "create_booking" and self.db:
                try:
                    input_args = tc.get("input", {})
                    booking_result = await async_create_booking(
                        self.db,
                        vehicle_number=input_args.get("vehicle_number", collected_slots.get("vehicle_number")),
                        service_type=input_args.get("service_type", collected_slots.get("service_type")),
                        preferred_date=input_args.get("preferred_date", collected_slots.get("preferred_date")),
                        caller_name=input_args.get("caller_name", collected_slots.get("caller_name")),
                        caller_number=input_args.get("caller_number"),
                        call_session_id=str(session.get("call_session_id", "")),
                    )
                    if booking_result.get("valid"):
                        booking_created = True
                        tc["result"] = booking_result
                    else:
                        tc["result"] = {"error": booking_result.get("error", "Unknown error")}
                except Exception as e:
                    logger.error("create_booking_error", extra={"error": str(e)})
                    tc["result"] = {"error": str(e)}

        # If LLM returned tool call but we didn't process it (no db), treat as not created
        if any(tc["name"] == "create_booking" for tc in tool_calls) and not self.db:
            logger.warning("create_booking_called_but_no_db_session")
            booking_created = False

        if booking_created:
            return {
                "response_text": response,
                "next_agent_state": "closing",
                "intent": session.get("intent"),
                "updated_slots": collected_slots,
                "tool_calls_made": [tc["name"] for tc in tool_calls],
                "slots_remaining": [],
                "action": "booking_confirmed",
                "llm_latency_ms": latency,
            }

        # Check if caller wants to change something
        lower = transcript.lower()
        if any(w in lower for w in ("no", "change", "modify", "இல்லை", "மாற்ற", "नहीं", "बदल")):
            return {
                "response_text": response,
                "next_agent_state": "collecting",
                "intent": session.get("intent"),
                "updated_slots": collected_slots,
                "tool_calls_made": [],
                "slots_remaining": list(collected_slots.keys()),
                "action": "return_to_collecting",
                "llm_latency_ms": latency,
            }

        return {
            "response_text": response,
            "next_agent_state": "confirming",
            "intent": session.get("intent"),
            "updated_slots": collected_slots,
            "tool_calls_made": [tc["name"] for tc in tool_calls],
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
    ) -> tuple[str, int, list[dict]]:
        """Call Claude Haiku and process tool use. Returns (response_text, latency_ms, tool_calls)."""
        messages = []
        for h in history[-20:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": current_transcript})

        start = time.monotonic()

        try:
            resp = await self.client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.LLM_MODEL,
                    "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
                    "temperature": settings.LLM_TEMPERATURE,
                    "system": system_prompt,
                    "messages": messages,
                    "tools": tools if tools else None,
                },
                timeout=3.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            logger.error("llm_call_failed", extra={"error": str(e)})
            return CLARIFICATION_MESSAGES.get(
                "en", "Sorry, please try again."
            ), latency, []

        latency = int((time.monotonic() - start) * 1000)

        # Extract text and tool use blocks
        response_text = ""
        tool_calls = []

        for block in data.get("content", []):
            if block["type"] == "text":
                response_text += block["text"]
            elif block["type"] == "tool_use":
                tool_name = block["name"]
                tool_input = block["input"]

                # Execute tool locally
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

        if not response_text and tool_calls:
            # If LLM only returned tool calls, make a follow-up call with results
            messages.append({"role": "assistant", "content": data["content"]})
            tool_results = []
            for tc in tool_calls:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": next(
                        (b["id"] for b in data["content"] if b.get("type") == "tool_use" and b["name"] == tc["name"]),
                        "unknown",
                    ),
                    "content": json.dumps(tc["result"]),
                })
            messages.append({"role": "user", "content": tool_results})

            try:
                resp2 = await self.client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": settings.LLM_MODEL,
                        "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
                        "temperature": settings.LLM_TEMPERATURE,
                        "system": system_prompt,
                        "messages": messages,
                        "tools": tools if tools else None,
                    },
                    timeout=3.0,
                )
                resp2.raise_for_status()
                data2 = resp2.json()
                for block in data2.get("content", []):
                    if block["type"] == "text":
                        response_text += block["text"]
            except Exception:
                pass

            latency = int((time.monotonic() - start) * 1000)

        return response_text, latency, tool_calls
