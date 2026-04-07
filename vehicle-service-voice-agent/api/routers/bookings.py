from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, get_redis, get_request_id, mask_phone_number
from api.models import Booking
from api.schemas import (
    BookingCancelRequest, BookingCreateRequest, BookingRescheduleRequest,
    ResponseEnvelope,
)
from api.services.booking_service import (
    SERVICE_LABELS, SERVICE_DURATIONS, create_booking, find_next_available_date,
    get_availability, transition_status,
)
from api.services.notification_service import enqueue_notification
from config import get_settings
from db import get_db

router = APIRouter(prefix="/api/v1/bookings", tags=["bookings"])
settings = get_settings()


@router.post("")
async def create_booking_endpoint(
    body: BookingCreateRequest,
    idempotency_key: uuid.UUID | None = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    booking, error_code, error_detail = await create_booking(
        db,
        vehicle_number=body.vehicle_number,
        service_type=body.service_type,
        preferred_date=body.preferred_date,
        caller_name=body.caller_name,
        caller_number=body.caller_number,
        call_session_id=body.call_session_id,
        notes=body.notes,
        idempotency_key=idempotency_key,
    )

    if error_code:
        status_map = {
            "SLOT_UNAVAILABLE": 409,
            "UNKNOWN_SERVICE_TYPE": 422,
            "INVALID_VEHICLE_NUMBER": 422,
            "DATE_IN_PAST": 422,
            "DATE_TOO_FAR": 422,
        }
        raise HTTPException(
            status_code=status_map.get(error_code, 400),
            detail={"code": error_code, **(error_detail or {})},
        )

    # Enqueue notification (non-blocking)
    try:
        redis = await get_redis()
        await enqueue_notification(
            db, redis,
            booking_id=booking.id,
            event_type="booking_confirmed",
            recipient_number=body.caller_number or "",
            language="en",
            template_vars={
                "booking_ref": booking.booking_ref,
                "customer_name": body.caller_name,
                "appointment_date": booking.appointment_date.strftime("%d %B %Y"),
                "service_label": SERVICE_LABELS.get(body.service_type, body.service_type),
                "workshop_address": settings.WORKSHOP_ADDRESS,
            },
            idempotency_key=f"booking_confirmed:{booking.id}",
        )
    except Exception:
        pass  # Non-blocking; notification failure doesn't block booking

    return ResponseEnvelope(
        data={
            "booking_id": str(booking.id),
            "booking_ref": booking.booking_ref,
            "vehicle_number": booking.vehicle_number,
            "service_type": booking.service_type,
            "service_label": SERVICE_LABELS.get(booking.service_type, booking.service_type),
            "appointment_date": booking.appointment_date.isoformat(),
            "appointment_slot": booking.appointment_slot.strftime("%H:%M"),
            "status": booking.status,
            "customer_name": booking.customer_name,
            "estimated_duration_minutes": booking.estimated_duration_minutes,
            "workshop_address": settings.WORKSHOP_ADDRESS,
            "created_at": booking.created_at.isoformat() if booking.created_at else None,
        },
        request_id=request_id,
    )


@router.get("/{booking_ref}")
async def get_booking(
    booking_ref: str,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(select(Booking).where(Booking.booking_ref == booking_ref))
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Get status history
    from api.models import BookingStatusHistory
    hist_result = await db.execute(
        select(BookingStatusHistory)
        .where(BookingStatusHistory.booking_id == booking.id)
        .order_by(BookingStatusHistory.changed_at)
    )
    history = [
        {"status": h.new_status, "at": h.changed_at.isoformat()}
        for h in hist_result.scalars().all()
    ]

    return ResponseEnvelope(
        data={
            "booking_id": str(booking.id),
            "booking_ref": booking.booking_ref,
            "vehicle_number": booking.vehicle_number,
            "service_type": booking.service_type,
            "service_label": SERVICE_LABELS.get(booking.service_type),
            "appointment_date": booking.appointment_date.isoformat(),
            "appointment_slot": booking.appointment_slot.strftime("%H:%M"),
            "status": booking.status,
            "customer_name": booking.customer_name,
            "caller_number_masked": mask_phone_number(booking.caller_number),
            "estimated_duration_minutes": booking.estimated_duration_minutes,
            "notes": booking.notes,
            "status_history": history,
            "created_at": booking.created_at.isoformat() if booking.created_at else None,
            "updated_at": booking.updated_at.isoformat() if booking.updated_at else None,
        },
        request_id=request_id,
    )


@router.get("/by-vehicle/{vehicle_number}")
async def get_by_vehicle(
    vehicle_number: str,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    from api.schemas import normalize_vehicle_number
    normalized = normalize_vehicle_number(vehicle_number)

    result = await db.execute(
        select(Booking)
        .where(
            Booking.vehicle_number == normalized,
            Booking.status.in_(["confirmed", "in_progress"]),
        )
        .order_by(Booking.created_at.desc())
        .limit(1)
    )
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(
            status_code=404,
            detail={"code": "BOOKING_NOT_FOUND", "message": f"No active booking found for {normalized}."},
        )

    return ResponseEnvelope(
        data={
            "booking_ref": booking.booking_ref,
            "status": booking.status,
            "appointment_date": booking.appointment_date.isoformat(),
            "service_label": SERVICE_LABELS.get(booking.service_type),
        },
        request_id=request_id,
    )


@router.get("/availability")
async def availability(
    date_from: date = Query(...),
    date_to: date = Query(...),
    service_type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    available, unavailable = await get_availability(db, date_from, date_to)
    return ResponseEnvelope(
        data={"available_dates": available, "unavailable_dates": unavailable},
        request_id=request_id,
    )


@router.patch("/{booking_ref}/reschedule")
async def reschedule(
    booking_ref: str,
    body: BookingRescheduleRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(select(Booking).where(Booking.booking_ref == booking_ref))
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status not in ("pending", "confirmed"):
        raise HTTPException(status_code=422, detail="Only pending/confirmed bookings can be rescheduled")

    from api.services.booking_service import get_or_create_capacity, find_earliest_slot

    cap = await get_or_create_capacity(db, body.new_date)
    if cap.is_holiday or cap.booked_slots >= cap.total_slots:
        raise HTTPException(status_code=409, detail="No slots available on requested date")

    previous_date = booking.appointment_date
    booking.appointment_date = body.new_date
    booking.appointment_slot = find_earliest_slot(cap.booked_slots)
    cap.booked_slots += 1

    from api.models import AuditLog, BookingStatusHistory
    db.add(AuditLog(
        entity_type="booking", entity_id=booking.id, action="RESCHEDULE",
        diff={"previous_date": previous_date.isoformat(), "new_date": body.new_date.isoformat()},
    ))
    await db.commit()

    return ResponseEnvelope(
        data={
            "booking_ref": booking.booking_ref,
            "previous_date": previous_date.isoformat(),
            "new_date": body.new_date.isoformat(),
            "new_slot": booking.appointment_slot.strftime("%H:%M"),
            "status": booking.status,
        },
        request_id=request_id,
    )


@router.delete("/{booking_ref}")
async def cancel_booking(
    booking_ref: str,
    body: BookingCancelRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(select(Booking).where(Booking.booking_ref == booking_ref))
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    error = await transition_status(db, booking, "cancelled", body.reason)
    if error:
        raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS_TRANSITION", "message": error})

    return ResponseEnvelope(
        data={
            "booking_ref": booking.booking_ref,
            "cancelled_at": booking.cancelled_at.isoformat() if booking.cancelled_at else None,
            "refund_applicable": False,
        },
        request_id=request_id,
    )


@router.get("")
async def list_bookings(
    status: str | None = Query(None),
    date: date | None = Query(None),
    service_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    from sqlalchemy import func

    query = select(Booking)
    count_query = select(func.count(Booking.id))

    if status:
        query = query.where(Booking.status == status)
        count_query = count_query.where(Booking.status == status)
    if date:
        query = query.where(Booking.appointment_date == date)
        count_query = count_query.where(Booking.appointment_date == date)
    if service_type:
        query = query.where(Booking.service_type == service_type)
        count_query = count_query.where(Booking.service_type == service_type)

    total = (await db.execute(count_query)).scalar() or 0
    results = await db.execute(
        query.order_by(Booking.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        {
            "booking_ref": b.booking_ref,
            "vehicle_number": b.vehicle_number,
            "service_type": b.service_type,
            "appointment_date": b.appointment_date.isoformat(),
            "status": b.status,
            "customer_name": b.customer_name,
        }
        for b in results.scalars().all()
    ]

    return ResponseEnvelope(
        data={"total": total, "page": page, "page_size": page_size, "items": items},
        request_id=request_id,
    )
