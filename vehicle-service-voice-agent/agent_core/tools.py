from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from api.services.booking_service import SERVICE_LABELS


def normalize_vehicle_number(raw_input: str) -> dict:
    """Normalize and validate an Indian vehicle registration number."""
    cleaned = re.sub(r"[\s\-]", "", raw_input).upper()
    pattern = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$")
    if pattern.match(cleaned):
        return {"valid": True, "normalized": cleaned}
    return {"valid": False, "error": f"'{raw_input}' is not a valid Indian vehicle number. Expected format: TN09AK1234"}


def validate_date(raw_date_string: str, reference_date: str | None = None) -> dict:
    """Parse and validate a preferred appointment date."""
    ref = date.fromisoformat(reference_date) if reference_date else date.today()
    tomorrow = ref + timedelta(days=1)
    max_date = ref + timedelta(days=30)

    raw = raw_date_string.lower().strip()

    # Handle relative dates
    if raw in ("tomorrow", "நாளை", "kal", "कल", "നാളെ"):
        target = ref + timedelta(days=1)
    elif raw in ("day after tomorrow", "நாளை மறுநாள்", "parson", "परसों", "മറ്റന്നാൾ"):
        target = ref + timedelta(days=2)
    elif "next" in raw or "அடுத்த" in raw or "अगले" in raw:
        # Try to find a weekday
        days_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        target = None
        for day_name, day_num in days_map.items():
            if day_name in raw:
                days_ahead = day_num - ref.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                target = ref + timedelta(days=days_ahead)
                break
        if not target:
            target = ref + timedelta(days=7)
    else:
        # Try ISO format
        try:
            target = date.fromisoformat(raw)
        except ValueError:
            # Try common formats
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%B %d %Y", "%d %b %Y"):
                try:
                    target = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                return {"valid": False, "error": f"Could not parse date: '{raw_date_string}'"}

    if target < tomorrow:
        return {"valid": False, "error": f"Date must be from {tomorrow.isoformat()} onwards."}
    if target > max_date:
        return {"valid": False, "error": f"Date must be within 30 days ({max_date.isoformat()})."}

    return {"valid": True, "date": target.isoformat()}


def check_service_type(description: str) -> dict:
    """Map a natural language service description to a canonical service type code."""
    desc = description.lower().strip()

    # Direct keyword matching
    mappings = {
        "general": "general_service",
        "regular": "general_service",
        "routine": "general_service",
        "service": "general_service",
        "oil": "oil_change",
        "engine oil": "oil_change",
        "brake": "brake_service",
        "braking": "brake_service",
        "ac": "ac_service",
        "air condition": "ac_service",
        "cooling": "ac_service",
        "tyre": "tyre_rotation",
        "tire": "tyre_rotation",
        "wheel": "tyre_rotation",
        "battery": "battery_check",
        "inspection": "full_inspection",
        "full check": "full_inspection",
        "body": "body_repair",
        "dent": "body_repair",
        "paint": "body_repair",
        "scratch": "body_repair",
        # Tamil keywords
        "பிரேக்": "brake_service",
        "எண்ணெய்": "oil_change",
        "டயர்": "tyre_rotation",
        "பேட்டரி": "battery_check",
        "AC": "ac_service",
        # Hindi keywords
        "ब्रेक": "brake_service",
        "ऑयल": "oil_change",
        "टायर": "tyre_rotation",
        "बैटरी": "battery_check",
    }

    for keyword, service_code in mappings.items():
        if keyword in desc:
            return {
                "valid": True,
                "service_type": service_code,
                "service_label": SERVICE_LABELS.get(service_code, service_code),
            }

    return {
        "valid": False,
        "error": f"Could not identify service type from: '{description}'",
        "available_types": list(SERVICE_LABELS.keys()),
    }


# Tool definitions for Claude tool use
AGENT_TOOLS = [
    {
        "name": "normalize_vehicle_number",
        "description": "Normalize and validate an Indian vehicle registration number",
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_input": {"type": "string", "description": "The vehicle number as spoken by caller"}
            },
            "required": ["raw_input"],
        },
    },
    {
        "name": "validate_date",
        "description": "Parse and validate a preferred appointment date. Returns ISO date or error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_date_string": {"type": "string", "description": "Date as spoken by caller"},
                "reference_date": {"type": "string", "description": "Today's date in ISO8601"},
            },
            "required": ["raw_date_string", "reference_date"],
        },
    },
    {
        "name": "check_service_type",
        "description": "Map a natural language service description to a canonical service type code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Service description from caller"}
            },
            "required": ["description"],
        },
    },
    {
        "name": "identify_intent",
        "description": "Identify the caller's intent from their utterance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["booking_new", "booking_status", "service_inquiry", "out_of_scope"],
                },
                "confidence": {"type": "number"},
            },
            "required": ["intent", "confidence"],
        },
    },
    {
        "name": "create_booking",
        "description": "Create a confirmed service booking and return a reference number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle_number": {"type": "string"},
                "service_type": {"type": "string"},
                "preferred_date": {"type": "string", "format": "date"},
                "caller_name": {"type": "string"},
                "caller_number": {"type": "string"},
            },
            "required": ["vehicle_number", "service_type", "preferred_date", "caller_name"],
        },
    },
    {
        "name": "lookup_booking_status",
        "description": "Look up an existing booking by reference number or vehicle number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_ref": {"type": "string"},
                "vehicle_number": {"type": "string"},
            },
        },
    },
]


TOOL_HANDLERS = {
    "normalize_vehicle_number": lambda args: normalize_vehicle_number(args["raw_input"]),
    "validate_date": lambda args: validate_date(args["raw_date_string"], args.get("reference_date")),
    "check_service_type": lambda args: check_service_type(args["description"]),
    "identify_intent": lambda args: args,  # Pass-through, LLM output is the result
}


# Async tool handlers that require database access
# These are handled separately in state_machine since they need db session
async def async_create_booking(
    db,
    vehicle_number: str,
    service_type: str,
    preferred_date: str,
    caller_name: str,
    caller_number: str | None = None,
    call_session_id: str | None = None,
) -> dict:
    """Async handler for create_booking tool that requires DB access."""
    from api.services.booking_service import create_booking as svc_create_booking
    from datetime import date

    try:
        preferred = date.fromisoformat(preferred_date)
    except ValueError:
        return {"valid": False, "error": f"Invalid date format: {preferred_date}"}

    booking, error_code, error_detail = await svc_create_booking(
        db,
        vehicle_number=vehicle_number,
        service_type=service_type,
        preferred_date=preferred,
        caller_name=caller_name,
        caller_number=caller_number,
    )

    if error_code:
        return {
            "valid": False,
            "error": error_detail.get("message", error_code) if error_detail else error_code,
            "code": error_code,
        }

    return {
        "valid": True,
        "booking_ref": booking.booking_ref,
        "booking_id": str(booking.id),
        "appointment_date": booking.appointment_date.isoformat(),
        "appointment_slot": booking.appointment_slot.strftime("%H:%M"),
        "status": booking.status,
    }


async def async_lookup_booking_status(
    db,
    booking_ref: str | None = None,
    vehicle_number: str | None = None,
) -> dict:
    """Async handler for lookup_booking_status tool that requires DB access."""
    from sqlalchemy import select
    from api.models import Booking

    if not booking_ref and not vehicle_number:
        return {
            "valid": False,
            "error": "Please provide either booking reference or vehicle number.",
        }

    query = select(Booking)
    if booking_ref:
        query = query.where(Booking.booking_ref == booking_ref)
    elif vehicle_number:
        normalized = normalize_vehicle_number(vehicle_number).get("normalized", vehicle_number)
        query = query.where(Booking.vehicle_number == normalized)

    result = await db.execute(query.order_by(Booking.created_at.desc()))
    booking = result.scalar_one_or_none()

    if not booking:
        return {
            "valid": False,
            "error": "No booking found with the provided details.",
        }

    return {
        "valid": True,
        "booking_ref": booking.booking_ref,
        "status": booking.status,
        "vehicle_number": booking.vehicle_number,
        "service_type": booking.service_type,
        "appointment_date": booking.appointment_date.isoformat(),
        "appointment_slot": booking.appointment_slot.strftime("%H:%M"),
        "customer_name": booking.customer_name,
    }
