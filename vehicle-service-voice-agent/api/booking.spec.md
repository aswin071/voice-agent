# booking.spec.md — Booking Management Module

---

## Feature: Vehicle Service Booking Management

### Goal
Provide a complete, idempotent, and transactionally safe booking lifecycle for vehicle service appointments — supporting creation via voice agent tool calls, status queries by booking reference or vehicle number, rescheduling, cancellation, and an availability calendar — with all operations exposed as RESTful FastAPI endpoints backed by PostgreSQL.

---

## Requirements

- The system SHALL create bookings atomically; a booking is only persisted if both the booking record AND the slot reservation succeed in the same transaction.
- All booking creation requests SHALL accept an `idempotency_key` (UUID); duplicate requests within 24 hours SHALL return the original booking without creating a duplicate.
- The system SHALL validate vehicle registration numbers against Indian format: `^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$` after normalization.
- The system SHALL enforce a service capacity limit per day (configurable; default 20 bookings/day).
- Bookings SHALL only be created for dates: tomorrow + 30 days ahead (no same-day bookings via voice).
- The system SHALL support the following service types: `general_service`, `brake_service`, `oil_change`, `ac_service`, `tyre_rotation`, `battery_check`, `full_inspection`, `body_repair`.
- Booking status transitions: `pending → confirmed → in_progress → completed | cancelled`.
- Only bookings in `pending` or `confirmed` status may be cancelled.
- Cancellation within 2 hours of booking creation is free; after that a cancellation log is stored.
- The system SHALL expose a date-based availability endpoint used by the ConfirmationAgent before finalizing.
- All booking mutations SHALL be written to `audit_logs`.

---

## API Contract

### `POST /api/v1/bookings`
Create a new service booking. Called by the ConfirmationAgent via tool call.

**Request Headers**: `X-Api-Key: sc_live_... | Authorization: Bearer <token>`
**Request Headers (idempotency)**: `Idempotency-Key: <uuid4>`

**Request**
```json
{
  "vehicle_number": "TN09AK1234",
  "service_type": "brake_service",
  "preferred_date": "2026-03-28",
  "caller_name": "Suresh Kumar",
  "caller_number": "+919876543210",
  "call_session_id": "550e8400-e29b-41d4-a716-446655440010",
  "notes": "Caller mentioned squeaking noise from front brakes"
}
```

**Response 201**
```json
{
  "success": true,
  "data": {
    "booking_id": "660e8400-e29b-41d4-a716-446655440020",
    "booking_ref": "SC-2026-0042",
    "vehicle_number": "TN09AK1234",
    "service_type": "brake_service",
    "service_label": "Brake Service",
    "appointment_date": "2026-03-28",
    "appointment_slot": "10:00",
    "status": "confirmed",
    "customer_name": "Suresh Kumar",
    "estimated_duration_minutes": 90,
    "workshop_address": "SpeedCare, 45 Anna Salai, Chennai 600002",
    "created_at": "2026-03-25T10:05:00Z"
  }
}
```

**Response 409** — Slot already taken
```json
{
  "success": false,
  "error": {
    "code": "SLOT_UNAVAILABLE",
    "message": "No slots available on 2026-03-28. Next available date: 2026-03-29.",
    "next_available_date": "2026-03-29"
  }
}
```

**Response 422** — Validation error
```json
{
  "success": false,
  "error": {
    "code": "INVALID_VEHICLE_NUMBER",
    "message": "Vehicle number 'TNXXXX' does not match Indian registration format.",
    "field": "vehicle_number"
  }
}
```

---

### `GET /api/v1/bookings/{booking_ref}`
Retrieve booking details by reference number. Used by BookingAgent status queries.

**Response 200**
```json
{
  "success": true,
  "data": {
    "booking_id": "660e8400-e29b-41d4-a716-446655440020",
    "booking_ref": "SC-2026-0042",
    "vehicle_number": "TN09AK1234",
    "service_type": "brake_service",
    "service_label": "Brake Service",
    "appointment_date": "2026-03-28",
    "appointment_slot": "10:00",
    "status": "confirmed",
    "customer_name": "Suresh Kumar",
    "caller_number_masked": "+91 XXXXX X3210",
    "estimated_duration_minutes": 90,
    "notes": "Caller mentioned squeaking noise from front brakes",
    "status_history": [
      { "status": "pending", "at": "2026-03-25T10:05:00Z" },
      { "status": "confirmed", "at": "2026-03-25T10:05:01Z" }
    ],
    "created_at": "2026-03-25T10:05:00Z",
    "updated_at": "2026-03-25T10:05:01Z"
  }
}
```

---

### `GET /api/v1/bookings/by-vehicle/{vehicle_number}`
Find the most recent active booking for a vehicle. Used by status-query intent path.

**Response 200**
```json
{
  "success": true,
  "data": {
    "booking_ref": "SC-2026-0042",
    "status": "confirmed",
    "appointment_date": "2026-03-28",
    "service_label": "Brake Service"
  }
}
```

**Response 404**
```json
{
  "success": false,
  "error": { "code": "BOOKING_NOT_FOUND", "message": "No active booking found for TN09AK1234." }
}
```

---

### `GET /api/v1/bookings/availability`
Query available slots for a date range. Used by agent before confirming a date.

**Query params**: `?date_from=2026-03-26&date_to=2026-03-31&service_type=brake_service`

**Response 200**
```json
{
  "success": true,
  "data": {
    "available_dates": [
      {
        "date": "2026-03-26",
        "slots_remaining": 8,
        "earliest_slot": "09:00"
      },
      {
        "date": "2026-03-28",
        "slots_remaining": 3,
        "earliest_slot": "14:00"
      },
      {
        "date": "2026-03-29",
        "slots_remaining": 15,
        "earliest_slot": "09:00"
      }
    ],
    "unavailable_dates": ["2026-03-27"]
  }
}
```

---

### `PATCH /api/v1/bookings/{booking_ref}/reschedule`
Reschedule a confirmed booking.

**Request Headers**: `Authorization: Bearer <token>` or `X-Api-Key: sc_live_...`

**Request**
```json
{
  "new_date": "2026-03-30",
  "reason": "caller_requested"
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "booking_ref": "SC-2026-0042",
    "previous_date": "2026-03-28",
    "new_date": "2026-03-30",
    "new_slot": "11:00",
    "status": "confirmed"
  }
}
```

---

### `DELETE /api/v1/bookings/{booking_ref}`
Cancel a booking.

**Request**
```json
{
  "reason": "caller_cancelled",
  "notes": "Caller said they'll come next month"
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "booking_ref": "SC-2026-0042",
    "cancelled_at": "2026-03-25T10:10:00Z",
    "refund_applicable": false
  }
}
```

---

### `GET /api/v1/bookings`
List all bookings with filters. Requires `admin` or `operator` role.

**Query params**: `?status=confirmed&date=2026-03-28&service_type=brake_service&page=1&page_size=20`

**Response 200**
```json
{
  "success": true,
  "data": {
    "total": 8,
    "page": 1,
    "page_size": 20,
    "items": [ { "...": "booking objects" } ]
  }
}
```

---

## Data Model

### `bookings`
```sql
CREATE TABLE bookings (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_ref                 VARCHAR(20) UNIQUE NOT NULL,  -- SC-YYYY-NNNN
    vehicle_number              VARCHAR(15) NOT NULL,
    service_type                VARCHAR(50) NOT NULL,
    appointment_date            DATE NOT NULL,
    appointment_slot            TIME NOT NULL DEFAULT '09:00',
    status                      VARCHAR(20) NOT NULL DEFAULT 'pending',
    customer_name               VARCHAR(100) NOT NULL,
    caller_number               VARCHAR(20),           -- encrypted at rest
    call_session_id             UUID REFERENCES call_sessions(id),
    notes                       TEXT,
    estimated_duration_minutes  INT DEFAULT 60,
    idempotency_key             UUID UNIQUE,
    cancelled_at                TIMESTAMPTZ,
    cancellation_reason         TEXT,
    completed_at                TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_bookings_vehicle    ON bookings(vehicle_number);
CREATE INDEX idx_bookings_date       ON bookings(appointment_date);
CREATE INDEX idx_bookings_status     ON bookings(status);
CREATE INDEX idx_bookings_ref        ON bookings(booking_ref);
CREATE INDEX idx_bookings_idempotent ON bookings(idempotency_key) WHERE idempotency_key IS NOT NULL;
```

### `booking_status_history`
```sql
CREATE TABLE booking_status_history (
    id          BIGSERIAL PRIMARY KEY,
    booking_id  UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    old_status  VARCHAR(20),
    new_status  VARCHAR(20) NOT NULL,
    changed_by  UUID,
    reason      TEXT,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_bsh_booking ON booking_status_history(booking_id);
```

### `daily_capacity`
```sql
CREATE TABLE daily_capacity (
    date            DATE PRIMARY KEY,
    total_slots     INT NOT NULL DEFAULT 20,
    booked_slots    INT NOT NULL DEFAULT 0,
    is_holiday      BOOLEAN DEFAULT FALSE,
    notes           TEXT
);
```

### `service_catalog`
```sql
CREATE TABLE service_catalog (
    code                    VARCHAR(50) PRIMARY KEY,
    label                   VARCHAR(100) NOT NULL,
    label_ta                VARCHAR(100),
    label_hi                VARCHAR(100),
    label_ml                VARCHAR(100),
    estimated_duration_min  INT NOT NULL DEFAULT 60,
    is_active               BOOLEAN DEFAULT TRUE,
    sort_order              INT DEFAULT 0
);

INSERT INTO service_catalog VALUES
  ('general_service',   'General Service',   'பொது சர்வீஸ்',    'जनरल सर्विस',    'ജനറൽ സർവ്വീസ്',  120, TRUE, 1),
  ('oil_change',        'Oil Change',        'எண்ணெய் மாற்றம்', 'ऑयल चेंज',       'ഓയൽ ചേഞ്ച്',     45, TRUE, 2),
  ('brake_service',     'Brake Service',     'பிரேக் சர்வீஸ்',  'ब्रेक सर्विस',   'ബ്രേക്ക് സർവ്വീസ്', 90, TRUE, 3),
  ('ac_service',        'AC Service',        'AC சர்வீஸ்',       'AC सर्विस',       'AC സർവ്വീസ്',    120, TRUE, 4),
  ('tyre_rotation',     'Tyre Rotation',     'டயர் ரோட்டேஷன்', 'टायर रोटेशन',   'ടയർ റൊട്ടേഷൻ',    60, TRUE, 5),
  ('battery_check',     'Battery Check',     'பேட்டரி சரிபார்', 'बैटरी चेक',     'ബാറ്ററി ചെക്ക്',  30, TRUE, 6),
  ('full_inspection',   'Full Inspection',   'முழு பரிசோதனை',   'फुल इंस्पेक्शन','ഫുൾ ഇൻസ്പെക്ഷൻ', 180, TRUE, 7),
  ('body_repair',       'Body Repair',       'பாடி ரிப்பேர்',   'बॉडी रिपेयर',   'ബോഡി റിപ്പയർ',   240, TRUE, 8);
```

---

## Business Logic

### Booking Reference Generation
```python
def generate_booking_ref(year: int, sequence: int) -> str:
    # SC-2026-0042 format
    return f"SC-{year}-{sequence:04d}"

# Sequence maintained via PostgreSQL sequence:
# CREATE SEQUENCE booking_seq START 1;
# nextval('booking_seq') called inside the booking transaction
```

### Slot Assignment Logic
1. Query `daily_capacity` for `preferred_date`: if `booked_slots >= total_slots` → return 409 with `next_available_date`.
2. If capacity available, assign earliest available 30-minute slot starting at 09:00, ending at 18:00.
3. Increment `booked_slots` in `daily_capacity` within the same transaction using `SELECT ... FOR UPDATE`.
4. Booking status set to `confirmed` immediately (no manual approval step in MVP).

### Idempotency
```python
async def create_booking(payload, idempotency_key):
    existing = await db.get_booking_by_idempotency(idempotency_key)
    if existing and (now() - existing.created_at) < timedelta(hours=24):
        return existing  # 200, not 201
    # else proceed with new booking
```

### Booking Reference Number Sequencing
- PostgreSQL sequence `booking_seq` ensures no duplicate refs.
- Format: `SC-{YYYY}-{zero-padded 4-digit seq}`.
- Sequence resets at year boundary via cron job on Jan 1.

### Status Transition Guard
```python
VALID_TRANSITIONS = {
    "pending":     {"confirmed", "cancelled"},
    "confirmed":   {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed":   set(),
    "cancelled":   set()
}

def transition(booking, new_status):
    if new_status not in VALID_TRANSITIONS[booking.status]:
        raise InvalidStatusTransitionError(booking.status, new_status)
```

---

## Edge Cases

| Scenario | Handling |
|---|---|
| Two concurrent requests for last slot on a date | `SELECT FOR UPDATE` on `daily_capacity` serializes; second request gets 409 |
| Caller provides date that is a Sunday / holiday | Check `daily_capacity.is_holiday`; respond with next available working day |
| Booking ref requested for cancelled booking | Return full record including `cancelled_at` and `cancellation_reason` |
| Vehicle has multiple bookings | `GET /by-vehicle` returns the most recent `confirmed` or `in_progress` booking |
| Idempotency key collision (same key, different vehicle) | Compare full payload; if mismatch, return 409 `IDEMPOTENCY_KEY_CONFLICT` |
| `appointment_date` in the past | 422 `DATE_IN_PAST` with earliest valid date |
| Service type not in catalog | 422 `UNKNOWN_SERVICE_TYPE`; list valid options in error detail |
| DB transaction deadlock | Retry up to 3 times with 100ms backoff; log as WARNING |

---

## Constraints

- **Transaction isolation**: `REPEATABLE READ` for booking creation to prevent phantom reads on slot count.
- **Capacity**: Default 20 bookings/day; configurable per `daily_capacity` record.
- **Booking window**: Tomorrow to +30 days from today; no same-day bookings.
- **Slot duration**: Minimum 30-minute intervals; working hours 09:00–18:00.
- **PII**: `caller_number` stored encrypted (AES-256-GCM) in DB; decrypted only for notification dispatch.
- **Soft delete**: Bookings never hard-deleted; cancelled bookings retained for 12 months.
- **API rate limit**: 30 booking creation requests/minute per API key (prevents agent loop bugs).

---

## Acceptance Criteria

- [ ] `POST /bookings` with valid payload creates booking and returns `SC-YYYY-NNNN` reference.
- [ ] Duplicate `POST /bookings` with same `Idempotency-Key` within 24h returns original booking (200, not 201).
- [ ] `POST /bookings` when day is full (20 bookings) returns 409 with `next_available_date`.
- [ ] `GET /bookings/by-vehicle/TN09AK1234` returns most recent active booking.
- [ ] `DELETE /bookings/SC-2026-0042` transitions status to `cancelled` and logs to `booking_status_history`.
- [ ] Cancelling a completed booking returns 422 `INVALID_STATUS_TRANSITION`.
- [ ] Two simultaneous POST requests for the same last slot: exactly one succeeds, one gets 409.
- [ ] All bookings for 2026-03-28 appear in `GET /bookings?date=2026-03-28`.
- [ ] Vehicle number `"tn 09 ak 1234"` normalized and validated correctly.
- [ ] `audit_logs` contains entries for every create, reschedule, and cancel operation.
