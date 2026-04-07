from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, get_redis, get_request_id, mask_phone_number
from api.models import Notification, NotificationTemplate
from api.schemas import (
    NotificationSendRequest, ResponseEnvelope, TemplateUpdateRequest,
)
from api.services.notification_service import enqueue_notification
from db import get_db

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.post("/send")
async def send_notification(
    body: NotificationSendRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    request_id: str = Depends(get_request_id),
):
    notification = await enqueue_notification(
        db, redis,
        booking_id=body.booking_id,
        event_type=body.event_type,
        recipient_number=body.recipient_number,
        language=body.language,
        template_vars=body.template_vars,
        idempotency_key=body.idempotency_key,
    )
    return ResponseEnvelope(
        success=True,
        data={
            "notification_id": str(notification.id),
            "status": notification.status,
            "queued_at": notification.queued_at.isoformat() if notification.queued_at else None,
            "estimated_delivery_seconds": 10,
        },
        request_id=request_id,
    )


@router.get("/{notification_id}")
async def get_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(
        select(Notification).where(Notification.id == notification_id)
    )
    n = result.scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found")

    return ResponseEnvelope(
        data={
            "notification_id": str(n.id),
            "booking_id": str(n.booking_id),
            "event_type": n.event_type,
            "recipient_masked": mask_phone_number(n.recipient_number),
            "language": n.language,
            "status": n.status,
            "provider": n.provider,
            "provider_message_id": n.provider_message_id,
            "queued_at": n.queued_at.isoformat() if n.queued_at else None,
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            "delivered_at": n.delivered_at.isoformat() if n.delivered_at else None,
            "retry_count": n.retry_count,
            "rendered_message": n.rendered_message,
        },
        request_id=request_id,
    )


@router.get("")
async def list_notifications(
    booking_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None),
    event_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    query = select(Notification)
    count_q = select(func.count(Notification.id))
    if booking_id:
        query = query.where(Notification.booking_id == booking_id)
        count_q = count_q.where(Notification.booking_id == booking_id)
    if status:
        query = query.where(Notification.status == status)
        count_q = count_q.where(Notification.status == status)
    if event_type:
        query = query.where(Notification.event_type == event_type)
        count_q = count_q.where(Notification.event_type == event_type)

    total = (await db.execute(count_q)).scalar() or 0
    results = await db.execute(
        query.order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        {
            "notification_id": str(n.id),
            "event_type": n.event_type,
            "status": n.status,
            "recipient_masked": mask_phone_number(n.recipient_number),
        }
        for n in results.scalars().all()
    ]

    return ResponseEnvelope(
        data={"total": total, "page": page, "page_size": page_size, "items": items},
        request_id=request_id,
    )


@router.post("/{notification_id}/retry")
async def retry_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(
        select(Notification).where(Notification.id == notification_id)
    )
    n = result.scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found")

    n.status = "queued"
    await db.commit()
    await redis.lpush("notifications:pending", str(n.id))

    return ResponseEnvelope(
        data={"notification_id": str(n.id), "status": "queued", "retry_count": n.retry_count},
        request_id=request_id,
    )


@router.get("/templates")
async def list_templates(
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(select(NotificationTemplate).order_by(NotificationTemplate.event_type))
    templates = [
        {
            "id": t.id,
            "event_type": t.event_type,
            "language": t.language,
            "template": t.template,
            "char_count": t.char_count,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in result.scalars().all()
    ]
    return ResponseEnvelope(data=templates, request_id=request_id)


@router.put("/templates/{template_id}")
async def update_template(
    template_id: str,
    body: TemplateUpdateRequest,
    db: AsyncSession = Depends(get_db),
    request_id: str = Depends(get_request_id),
):
    result = await db.execute(
        select(NotificationTemplate).where(NotificationTemplate.id == template_id)
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    tpl.template = body.template
    tpl.char_count = len(body.template.encode("utf-16-le")) // 2
    await db.commit()

    return ResponseEnvelope(
        data={
            "template_id": tpl.id,
            "char_count": tpl.char_count,
            "updated_at": tpl.updated_at.isoformat() if tpl.updated_at else None,
        },
        request_id=request_id,
    )
