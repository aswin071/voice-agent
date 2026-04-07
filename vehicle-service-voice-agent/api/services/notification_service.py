from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from jinja2 import Template, StrictUndefined, UndefinedError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Notification, NotificationTemplate
from config import get_settings

logger = logging.getLogger("speedcare.notification")
settings = get_settings()

RETRY_DELAYS = [60, 300, 900]  # 1m, 5m, 15m


def render_template(template_str: str, vars: dict) -> str:
    tpl = Template(template_str, undefined=StrictUndefined)
    rendered = tpl.render(**vars)
    char_count = len(rendered.encode("utf-16-le")) // 2
    if char_count > 160:
        logger.warning("sms_over_segment_limit", extra={"char_count": char_count})
    return rendered


async def enqueue_notification(
    db: AsyncSession,
    redis,
    booking_id: uuid.UUID,
    event_type: str,
    recipient_number: str,
    language: str,
    template_vars: dict,
    idempotency_key: str,
) -> Notification:
    # Check idempotency
    result = await db.execute(
        select(Notification).where(Notification.idempotency_key == idempotency_key)
    )
    existing = result.scalar_one_or_none()
    if existing and existing.status != "failed":
        return existing

    # Fetch template
    result = await db.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.event_type == event_type,
            NotificationTemplate.language == language,
            NotificationTemplate.is_active == True,
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        # Fallback to English
        result = await db.execute(
            select(NotificationTemplate).where(
                NotificationTemplate.event_type == event_type,
                NotificationTemplate.language == "en",
                NotificationTemplate.is_active == True,
            )
        )
        template = result.scalar_one_or_none()

    if not template:
        raise ValueError(f"No template found for {event_type}/{language}")

    try:
        rendered = render_template(template.template, template_vars)
    except UndefinedError as e:
        logger.error("template_render_error", extra={"error": str(e), "event_type": event_type})
        raise

    notification = Notification(
        booking_id=booking_id,
        event_type=event_type,
        recipient_number=recipient_number,
        language=language,
        status="queued",
        rendered_message=rendered,
        idempotency_key=idempotency_key,
        provider=settings.SMS_PROVIDER,
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)

    # Push to Redis queue
    await redis.lpush("notifications:pending", str(notification.id))

    return notification


async def send_sms_exotel(phone: str, message: str) -> tuple[bool, str | None]:
    if not settings.EXOTEL_API_KEY:
        logger.warning("exotel_not_configured")
        return True, "mock_exotel_id"  # Mock for dev

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://{settings.EXOTEL_SID}:{settings.EXOTEL_API_TOKEN}@api.exotel.com/v1/Accounts/{settings.EXOTEL_SID}/Sms/send.json",
            data={
                "From": settings.EXOTEL_SENDER_NUMBER,
                "To": phone,
                "Body": message,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            return True, data.get("SMSMessage", {}).get("Sid")
        return False, None


async def send_sms_twilio(phone: str, message: str) -> tuple[bool, str | None]:
    if not settings.TWILIO_ACCOUNT_SID:
        logger.warning("twilio_not_configured")
        return True, "mock_twilio_id"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            data={
                "From": settings.TWILIO_FROM_NUMBER,
                "To": phone,
                "Body": message,
            },
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return True, data.get("sid")
        return False, None


async def dispatch_notification(db: AsyncSession, notification: Notification) -> bool:
    notification.status = "sending"
    await db.commit()

    send_fn = send_sms_exotel if settings.SMS_PROVIDER == "exotel" else send_sms_twilio

    try:
        success, msg_id = await send_fn(notification.recipient_number, notification.rendered_message)
    except Exception as e:
        logger.error("sms_dispatch_error", extra={"error": str(e), "notification_id": str(notification.id)})
        success = False
        msg_id = None

    if success:
        notification.status = "delivered"
        notification.provider_message_id = msg_id
        notification.delivered_at = datetime.now(timezone.utc)
        notification.sent_at = datetime.now(timezone.utc)
    else:
        notification.retry_count += 1
        if notification.retry_count >= 3:
            notification.status = "failed"
            notification.failed_at = datetime.now(timezone.utc)
            logger.error("notification_permanently_failed", extra={"notification_id": str(notification.id)})
        else:
            notification.status = "queued"
            delay = RETRY_DELAYS[min(notification.retry_count - 1, len(RETRY_DELAYS) - 1)]
            notification.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

    await db.commit()
    return success
