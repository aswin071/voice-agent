from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Booking, BookingStatusHistory, DailyCapacity, ServiceCatalog, AuditLog
from config import get_settings

settings = get_settings()

VALID_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}

SERVICE_LABELS = {
    "general_service": "General Service",
    "oil_change": "Oil Change",
    "brake_service": "Brake Service",
    "ac_service": "AC Service",
    "tyre_rotation": "Tyre Rotation",
    "battery_check": "Battery Check",
    "full_inspection": "Full Inspection",
    "body_repair": "Body Repair",
}

SERVICE_DURATIONS = {
    "general_service": 120,
    "oil_change": 45,
    "brake_service": 90,
    "ac_service": 120,
    "tyre_rotation": 60,
    "battery_check": 30,
    "full_inspection": 180,
    "body_repair": 240,
}


async def get_or_create_capacity(db: AsyncSession, target_date: date) -> DailyCapacity:
    result = await db.execute(
        select(DailyCapacity).where(DailyCapacity.date == target_date).with_for_update()
    )
    cap = result.scalar_one_or_none()
    if not cap:
        cap = DailyCapacity(
            date=target_date,
            total_slots=settings.DAILY_BOOKING_CAPACITY,
            booked_slots=0,
        )
        db.add(cap)
        await db.flush()
    return cap


async def generate_booking_ref(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    result = await db.execute(
        select(func.count(Booking.id)).where(
            Booking.created_at >= datetime(year, 1, 1, tzinfo=timezone.utc)
        )
    )
    seq = (result.scalar() or 0) + 1
    return f"SC-{year}-{seq:04d}"


def find_earliest_slot(booked_slots: int) -> time:
    """Return next available 30-min slot starting at 09:00."""
    slot_index = booked_slots
    hour = 9 + (slot_index * 30) // 60
    minute = (slot_index * 30) % 60
    if hour >= 18:
        hour, minute = 17, 30
    return time(hour, minute)


async def create_booking(
    db: AsyncSession,
    vehicle_number: str,
    service_type: str,
    preferred_date: date,
    caller_name: str,
    caller_number: str | None = None,
    call_session_id: uuid.UUID | None = None,
    notes: str | None = None,
    idempotency_key: uuid.UUID | None = None,
) -> tuple[Booking | None, str | None, dict | None]:
    """Returns (booking, error_code, error_detail)."""

    # Idempotency check
    if idempotency_key:
        result = await db.execute(
            select(Booking).where(Booking.idempotency_key == idempotency_key)
        )
        existing = result.scalar_one_or_none()
        if existing:
            if (datetime.now(timezone.utc) - existing.created_at.replace(tzinfo=timezone.utc)) < timedelta(hours=24):
                return existing, None, None

    # Validate service type
    if service_type not in SERVICE_LABELS:
        return None, "UNKNOWN_SERVICE_TYPE", {
            "message": f"Unknown service type: {service_type}",
            "valid_types": list(SERVICE_LABELS.keys()),
        }

    # Validate date range
    tomorrow = date.today() + timedelta(days=1)
    max_date = date.today() + timedelta(days=settings.BOOKING_WINDOW_DAYS)
    if preferred_date < tomorrow:
        return None, "DATE_IN_PAST", {
            "message": f"Cannot book for past dates. Earliest: {tomorrow.isoformat()}",
            "earliest_valid_date": tomorrow.isoformat(),
        }
    if preferred_date > max_date:
        return None, "DATE_TOO_FAR", {
            "message": f"Cannot book beyond {max_date.isoformat()}",
        }

    # Check capacity
    cap = await get_or_create_capacity(db, preferred_date)
    if cap.is_holiday:
        next_avail = await find_next_available_date(db, preferred_date)
        return None, "SLOT_UNAVAILABLE", {
            "message": f"{preferred_date} is a holiday. Next available: {next_avail}",
            "next_available_date": next_avail,
        }
    if cap.booked_slots >= cap.total_slots:
        next_avail = await find_next_available_date(db, preferred_date)
        return None, "SLOT_UNAVAILABLE", {
            "message": f"No slots available on {preferred_date}. Next available: {next_avail}",
            "next_available_date": next_avail,
        }

    slot_time = find_earliest_slot(cap.booked_slots)
    booking_ref = await generate_booking_ref(db)
    duration = SERVICE_DURATIONS.get(service_type, 60)

    booking = Booking(
        booking_ref=booking_ref,
        vehicle_number=vehicle_number,
        service_type=service_type,
        appointment_date=preferred_date,
        appointment_slot=slot_time,
        status="confirmed",
        customer_name=caller_name,
        caller_number=caller_number,
        call_session_id=call_session_id,
        notes=notes,
        estimated_duration_minutes=duration,
        idempotency_key=idempotency_key,
    )
    db.add(booking)

    cap.booked_slots += 1

    # Status history
    history = BookingStatusHistory(
        booking_id=booking.id,
        old_status=None,
        new_status="confirmed",
        reason="auto_confirmed_on_creation",
    )
    db.add(history)

    # Audit log
    audit = AuditLog(
        entity_type="booking",
        entity_id=booking.id,
        action="CREATE",
        diff={"vehicle_number": vehicle_number, "service_type": service_type, "date": preferred_date.isoformat()},
    )
    db.add(audit)

    await db.commit()
    await db.refresh(booking)
    return booking, None, None


async def find_next_available_date(db: AsyncSession, after: date) -> str:
    for i in range(1, 31):
        d = after + timedelta(days=i)
        cap = await get_or_create_capacity(db, d)
        if not cap.is_holiday and cap.booked_slots < cap.total_slots:
            return d.isoformat()
    return (after + timedelta(days=1)).isoformat()


async def transition_status(db: AsyncSession, booking: Booking, new_status: str, reason: str | None = None) -> str | None:
    if new_status not in VALID_TRANSITIONS.get(booking.status, set()):
        return f"Cannot transition from {booking.status} to {new_status}"

    old_status = booking.status
    booking.status = new_status
    if new_status == "cancelled":
        booking.cancelled_at = datetime.now(timezone.utc)
        booking.cancellation_reason = reason
    elif new_status == "completed":
        booking.completed_at = datetime.now(timezone.utc)

    history = BookingStatusHistory(
        booking_id=booking.id,
        old_status=old_status,
        new_status=new_status,
        reason=reason,
    )
    db.add(history)

    audit = AuditLog(
        entity_type="booking",
        entity_id=booking.id,
        action=new_status.upper(),
        diff={"old_status": old_status, "new_status": new_status},
    )
    db.add(audit)

    await db.commit()
    return None


async def get_availability(db: AsyncSession, date_from: date, date_to: date) -> tuple[list, list]:
    available = []
    unavailable = []
    d = date_from
    while d <= date_to:
        cap = await get_or_create_capacity(db, d)
        remaining = cap.total_slots - cap.booked_slots
        if cap.is_holiday or remaining <= 0:
            unavailable.append(d.isoformat())
        else:
            available.append({
                "date": d.isoformat(),
                "slots_remaining": remaining,
                "earliest_slot": find_earliest_slot(cap.booked_slots).strftime("%H:%M"),
            })
        d += timedelta(days=1)
    return available, unavailable
