# notification.spec.md — Notification & Communication Module

---

## Feature: Notification & Communication System

### Goal
Deliver reliable, multilingual, templated SMS notifications to callers at key booking lifecycle events — booking confirmation, reminders (24h before appointment), and cancellation acknowledgement — using async background task processing with guaranteed at-least-once delivery, idempotency, and full delivery audit trails.

---

## Requirements

- The system SHALL send an SMS confirmation immediately (< 30 seconds) after a booking is created.
- The system SHALL send a reminder SMS 24 hours before the appointment via a scheduled background job.
- The system SHALL send a cancellation SMS when a booking is cancelled with status `confirmed` or `pending`.
- All SMS SHALL be sent in the caller's detected language (Tamil, Hindi, English, Malayalam).
- All notification requests SHALL be enqueued asynchronously; the booking API SHALL NOT block on SMS delivery.
- The system SHALL use Exotel or Twilio as the SMS provider (configurable via env var `SMS_PROVIDER`).
- All outbound notifications SHALL be tracked in `notifications` table with delivery status.
- Failed deliveries SHALL be retried with exponential backoff: 3 retries, 1m / 5m / 15m delays.
- After 3 failed retries, notification marked `failed`; ops alerted via Sentry.
- Duplicate sends SHALL be prevented by idempotency key per `(booking_id, event_type)` pair.
- Notification templates SHALL be stored in DB to allow content updates without deployment.
- Phone numbers SHALL be validated to E.164 format before dispatch.
- SMS content SHALL not exceed 160 characters per segment (use Unicode-aware char counting for Tamil/Malayalam).

---

## API Contract

### `POST /api/v1/notifications/send`
Enqueue a notification for delivery. Typically called internally by booking service.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request**
```json
{
  "booking_id": "660e8400-e29b-41d4-a716-446655440020",
  "event_type": "booking_confirmed",
  "recipient_number": "+919876543210",
  "language": "ta",
  "template_vars": {
    "booking_ref": "SC-2026-0042",
    "customer_name": "Suresh",
    "appointment_date": "28 March 2026",
    "service_label": "Brake Service",
    "workshop_address": "SpeedCare, Anna Salai, Chennai"
  },
  "idempotency_key": "booking_confirmed:660e8400-e29b-41d4-a716-446655440020"
}
```

**Response 202** — Accepted and queued (non-blocking)
```json
{
  "success": true,
  "data": {
    "notification_id": "770e8400-e29b-41d4-a716-446655440030",
    "status": "queued",
    "queued_at": "2026-03-25T10:05:02Z",
    "estimated_delivery_seconds": 10
  }
}
```

---

### `GET /api/v1/notifications/{notification_id}`
Get delivery status of a specific notification.

**Response 200**
```json
{
  "success": true,
  "data": {
    "notification_id": "770e8400-e29b-41d4-a716-446655440030",
    "booking_id": "660e8400-e29b-41d4-a716-446655440020",
    "booking_ref": "SC-2026-0042",
    "event_type": "booking_confirmed",
    "recipient_masked": "+91 XXXXX X3210",
    "language": "ta",
    "status": "delivered",
    "provider": "exotel",
    "provider_message_id": "EX-MSG-abc123",
    "queued_at": "2026-03-25T10:05:02Z",
    "sent_at": "2026-03-25T10:05:08Z",
    "delivered_at": "2026-03-25T10:05:12Z",
    "retry_count": 0,
    "rendered_message": "வணக்கம் Suresh! SC-2026-0042 உங்கள் Brake Service 28 March 2026 அன்று confirm ஆகிவிட்டது. SpeedCare, Anna Salai."
  }
}
```

---

### `GET /api/v1/notifications`
List notifications with filters. Requires `admin` or `operator` role.

**Query params**: `?booking_id=...&status=failed&event_type=booking_confirmed&page=1&page_size=20`

**Response 200**
```json
{
  "success": true,
  "data": {
    "total": 3,
    "page": 1,
    "page_size": 20,
    "items": [ { "...": "notification objects (masked)" } ]
  }
}
```

---

### `POST /api/v1/notifications/{notification_id}/retry`
Manually trigger a retry for a failed notification.

**Request Headers**: `Authorization: Bearer <operator_token>`

**Response 200**
```json
{
  "success": true,
  "data": {
    "notification_id": "...",
    "status": "queued",
    "retry_count": 4
  }
}
```

---

### `GET /api/v1/notifications/templates`
List all notification templates.

**Response 200**
```json
{
  "success": true,
  "data": [
    {
      "id": "booking_confirmed_ta",
      "event_type": "booking_confirmed",
      "language": "ta",
      "template": "வணக்கம் {{customer_name}}! {{booking_ref}} உங்கள் {{service_label}} {{appointment_date}} அன்று confirm. {{workshop_address}}. -SpeedCare",
      "char_count": 120,
      "updated_at": "2026-03-20T09:00:00Z"
    }
  ]
}
```

---

### `PUT /api/v1/notifications/templates/{template_id}`
Update a notification template (without deployment).

**Request Headers**: `Authorization: Bearer <admin_token>`

**Request**
```json
{
  "template": "வணக்கம் {{customer_name}}! {{booking_ref}} {{service_label}} {{appointment_date}}. SpeedCare: {{workshop_address}}",
  "change_reason": "Shortened for 1 SMS segment"
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "template_id": "booking_confirmed_ta",
    "char_count": 112,
    "updated_at": "2026-03-25T10:30:00Z"
  }
}
```

---

## Data Model

### `notifications`
```sql
CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id          UUID NOT NULL REFERENCES bookings(id),
    event_type          VARCHAR(50) NOT NULL,   -- booking_confirmed | booking_reminder | booking_cancelled
    recipient_number    VARCHAR(20) NOT NULL,   -- encrypted at rest
    language            VARCHAR(10) NOT NULL DEFAULT 'ta',
    status              VARCHAR(20) NOT NULL DEFAULT 'queued',  -- queued | sending | delivered | failed | skipped
    provider            VARCHAR(20),            -- exotel | twilio
    provider_message_id VARCHAR(100),
    rendered_message    TEXT NOT NULL,
    idempotency_key     VARCHAR(200) UNIQUE NOT NULL,
    retry_count         INT NOT NULL DEFAULT 0,
    next_retry_at       TIMESTAMPTZ,
    queued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at             TIMESTAMPTZ,
    delivered_at        TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    error_detail        JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notifications_booking    ON notifications(booking_id);
CREATE INDEX idx_notifications_status     ON notifications(status, next_retry_at) WHERE status IN ('queued', 'failed');
CREATE INDEX idx_notifications_idempotent ON notifications(idempotency_key);
```

### `notification_templates`
```sql
CREATE TABLE notification_templates (
    id              VARCHAR(100) PRIMARY KEY,  -- e.g. "booking_confirmed_ta"
    event_type      VARCHAR(50) NOT NULL,
    language        VARCHAR(10) NOT NULL,
    template        TEXT NOT NULL,
    char_count      INT,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_type, language)
);
```

**Seed Data: Templates**
```sql
INSERT INTO notification_templates (id, event_type, language, template, char_count) VALUES
-- Tamil
('booking_confirmed_ta', 'booking_confirmed', 'ta',
 'வணக்கம் {{customer_name}}! {{booking_ref}} உங்கள் {{service_label}} {{appointment_date}} confirm. {{workshop_address}} -SpeedCare', 118),

('booking_reminder_ta', 'booking_reminder', 'ta',
 'நினைவூட்டல்: {{booking_ref}} உங்கள் {{service_label}} நாளை ({{appointment_date}}) உள்ளது. SpeedCare', 96),

('booking_cancelled_ta', 'booking_cancelled', 'ta',
 'உங்கள் booking {{booking_ref}} ({{service_label}}, {{appointment_date}}) ரத்து செய்யப்பட்டது. -SpeedCare', 104),

-- Hindi
('booking_confirmed_hi', 'booking_confirmed', 'hi',
 'नमस्ते {{customer_name}}! {{booking_ref}} आपकी {{service_label}} {{appointment_date}} confirm है। {{workshop_address}} -SpeedCare', 120),

('booking_reminder_hi', 'booking_reminder', 'hi',
 'याद दिलाना: {{booking_ref}} आपकी {{service_label}} कल ({{appointment_date}}) है। SpeedCare', 89),

('booking_cancelled_hi', 'booking_cancelled', 'hi',
 'आपकी booking {{booking_ref}} ({{service_label}}, {{appointment_date}}) रद्द कर दी गई। -SpeedCare', 97),

-- English
('booking_confirmed_en', 'booking_confirmed', 'en',
 'Hi {{customer_name}}! Booking {{booking_ref}} confirmed: {{service_label}} on {{appointment_date}}. {{workshop_address}} -SpeedCare', 122),

('booking_reminder_en', 'booking_reminder', 'en',
 'Reminder: {{booking_ref}} {{service_label}} tomorrow ({{appointment_date}}). SpeedCare', 78),

('booking_cancelled_en', 'booking_cancelled', 'en',
 'Booking {{booking_ref}} ({{service_label}}, {{appointment_date}}) has been cancelled. -SpeedCare', 93),

-- Malayalam
('booking_confirmed_ml', 'booking_confirmed', 'ml',
 'നമസ്കാരം {{customer_name}}! {{booking_ref}} {{service_label}} {{appointment_date}} confirm ആയി. {{workshop_address}} -SpeedCare', 118),

('booking_reminder_ml', 'booking_reminder', 'ml',
 'ഓർമ്മപ്പെടുത്തൽ: {{booking_ref}} {{service_label}} നാളെ ({{appointment_date}}). SpeedCare', 88),

('booking_cancelled_ml', 'booking_cancelled', 'ml',
 'നിങ്ങളുടെ booking {{booking_ref}} ({{service_label}}, {{appointment_date}}) റദ്ദ് ചെയ്തു. -SpeedCare', 96);
```

---

## Business Logic

### Notification Dispatch Flow

```
1. Booking service emits internal event: BookingConfirmed(booking_id, caller_number, language, slots)
2. NotificationService.enqueue() called:
   a. Build idempotency_key = f"{event_type}:{booking_id}"
   b. Check DB for existing notification with same idempotency_key → if found and status != failed, return existing
   c. Fetch template: notification_templates WHERE event_type=? AND language=?
   d. Render template: replace {{vars}} with actual values (Jinja2)
   e. Validate rendered_message length: warn if > 160 chars (standard SMS segment)
   f. Insert notification record with status='queued'
   g. Push task to Redis queue (key: "notifications:pending")
3. Background worker (asyncio task, polling Redis every 500ms):
   a. Pop notification_id from queue
   b. Fetch notification record
   c. Mark status='sending'
   d. Call SMS provider API (Exotel or Twilio based on env var)
   e. On success: update status='delivered', provider_message_id, delivered_at
   f. On failure: increment retry_count; calculate next_retry_at (1m/5m/15m); re-enqueue
4. Retry worker (runs every 60 seconds):
   a. SELECT * FROM notifications WHERE status='failed' AND retry_count < 3 AND next_retry_at <= NOW()
   b. Re-enqueue each into Redis queue
   c. After 3rd failure: status='failed', alert Sentry
```

### Template Rendering
```python
from jinja2 import Template, StrictUndefined

def render_template(template_str: str, vars: dict) -> str:
    """Raises UndefinedError if a required variable is missing in vars."""
    tpl = Template(template_str, undefined=StrictUndefined)
    rendered = tpl.render(**vars)
    # SMS segment validation
    char_count = len(rendered.encode('utf-16-le')) // 2  # Unicode-aware
    if char_count > 160:
        logger.warning("sms_over_segment_limit", char_count=char_count)
    return rendered
```

### 24-Hour Reminder Scheduler
```python
# Runs as a daily cron job at 10:00 AM
async def send_daily_reminders():
    tomorrow = date.today() + timedelta(days=1)
    bookings = await db.get_bookings_for_date(tomorrow, status="confirmed")
    for booking in bookings:
        await notification_service.enqueue(
            booking_id=booking.id,
            event_type="booking_reminder",
            recipient_number=booking.caller_number,
            language=booking.language or "en",
            template_vars={...}
        )
```

### Fallback Language
- If no template found for detected language → fall back to `en` (English).
- Log `language_fallback` warning with `original_language`, `booking_id`.

---

## Edge Cases

| Scenario | Handling |
|---|---|
| SMS provider API returns HTTP 429 | Treat as temporary failure; retry with backoff; do NOT count as retry_count (separate rate-limit retry) |
| Invalid/inactive phone number (provider error `ERR_INVALID_NUMBER`) | Mark status='skipped' with error detail; no retry; log to Sentry |
| Template variable missing at render time | Catch `UndefinedError`; log error; mark notification failed; alert ops |
| Booking cancelled before reminder sent | Check booking status at reminder send time; if cancelled, mark notification 'skipped' |
| SMS provider outage > 15 minutes | All notifications queue in Redis; automatic retry when provider recovers |
| Duplicate webhook from provider (delivered callback twice) | Idempotent `UPDATE ... WHERE status != 'delivered'`; second callback is no-op |
| Phone number in non-E.164 format from STT | Normalize using `phonenumbers` library before storing; reject if unparseable |
| Unicode SMS (Tamil/Malayalam) > 70 chars | Single segment = 70 chars for Unicode; accept multi-segment (provider handles split) |

---

## Constraints

- **Async**: All SMS dispatches are fire-and-forget from booking API's perspective. Booking API returns within 100ms of enqueue.
- **Delivery guarantee**: At-least-once delivery (idempotency key prevents duplicates on retry).
- **PII**: Recipient phone numbers stored encrypted in `notifications` table; decrypted only at dispatch time.
- **Template updates**: Template changes take effect immediately (fetched from DB at render time, not cached).
- **Rate limiting**: Exotel/Twilio limits enforced; max 10 SMS/second across all workers (Redis counter).
- **Retention**: Notification records retained 12 months; then archived to cold storage.
- **Monitoring**: Failed notification count per hour exposed as Prometheus gauge `speedcare_notifications_failed_total`.

---

## Acceptance Criteria

- [ ] `POST /bookings` triggers notification enqueue within 100ms (non-blocking verification via `notification_id` in booking response).
- [ ] Notification `status=delivered` within 30 seconds of booking creation in integration test.
- [ ] Duplicate `POST /notifications/send` with same `idempotency_key` returns existing notification (202 → 200).
- [ ] Tamil SMS template renders correctly with all 5 variables and stays under 160 Unicode chars.
- [ ] Failed delivery (mocked provider 500) retried 3 times at 1m/5m/15m; then marked `status=failed`.
- [ ] Reminder job correctly identifies all `confirmed` bookings for tomorrow and enqueues reminders.
- [ ] Cancelled booking does not receive reminder SMS (scheduler skips cancelled bookings).
- [ ] Missing template variable (`{{customer_name}}` removed from template) raises error; notification marked failed; Sentry alerted.
- [ ] `GET /notifications?status=failed` lists all failed notifications with `retry_count=3`.
- [ ] Provider phone number validation error marks notification `status=skipped` with no further retries.
