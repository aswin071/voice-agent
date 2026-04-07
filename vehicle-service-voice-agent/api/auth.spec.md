# auth.spec.md — Authentication & Authorization Module

---

## Feature: Authentication & Authorization

### Goal
Provide a secure, stateless authentication layer for all FastAPI backend endpoints and LiveKit room access tokens, supporting both human operators (dashboard users) and internal service-to-service calls, with JWT-based session management and API key authentication for machine clients.

---

## Requirements

- The system SHALL authenticate human operators (admin dashboard) using username/password → JWT access + refresh token flow.
- The system SHALL authenticate agent workers and internal services using long-lived API keys (hashed with bcrypt in DB).
- The system SHALL generate and validate LiveKit room access tokens (signed JWTs) scoped to a single `call_sid`.
- All JWT access tokens SHALL expire in 15 minutes; refresh tokens in 7 days.
- Refresh token rotation SHALL be enforced: each refresh issues a new refresh token and invalidates the old one.
- API keys SHALL be prefixed (`sc_live_`, `sc_test_`) and stored as `bcrypt(key)` in DB — never in plaintext.
- Failed authentication attempts SHALL be rate-limited: 5 failures per 15-minute window per IP → 429.
- All auth events (login, logout, token refresh, key creation, key revocation) SHALL be written to `audit_logs`.
- Password policy: minimum 12 characters, at least one uppercase, one digit, one special character.
- The system SHALL support role-based access control (RBAC) with roles: `admin`, `operator`, `service`.

---

## API Contract

### `POST /api/v1/auth/login`
Authenticate a human operator.

**Request**
```json
{
  "email": "admin@speedcare.in",
  "password": "S3cur3Pass!@#"
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "Bearer",
    "expires_in": 900,
    "user": {
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "email": "admin@speedcare.in",
      "role": "admin",
      "name": "Raj Kumar"
    }
  }
}
```

**Response 401** — Invalid credentials (no detail on which field is wrong)
```json
{
  "success": false,
  "error": {
    "code": "INVALID_CREDENTIALS",
    "message": "Invalid email or password."
  }
}
```

---

### `POST /api/v1/auth/refresh`
Rotate refresh token, issue new access token.

**Request**
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "expires_in": 900
  }
}
```

---

### `POST /api/v1/auth/logout`
Invalidate the refresh token (add to Redis blocklist).

**Request Headers**: `Authorization: Bearer <access_token>`

**Request**
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Response 200**
```json
{ "success": true, "data": { "message": "Logged out successfully." } }
```

---

### `POST /api/v1/auth/livekit-token`
Generate a short-lived LiveKit room token for an agent worker joining a call room. Called by the agent orchestrator immediately before connecting to a room.

**Request Headers**: `X-Api-Key: sc_live_...`

**Request**
```json
{
  "call_sid": "LK_ROOM_abc123",
  "participant_identity": "agent-worker-1",
  "grants": ["roomJoin", "roomRecord", "canPublish", "canSubscribe"],
  "ttl_seconds": 7200
}
```

**Response 200**
```json
{
  "success": true,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "room_name": "LK_ROOM_abc123",
    "expires_at": "2026-03-25T12:00:00Z"
  }
}
```

---

### `POST /api/v1/auth/api-keys`
Create a new service API key.

**Request Headers**: `Authorization: Bearer <admin_access_token>`

**Request**
```json
{
  "name": "agent-worker-prod-1",
  "role": "service",
  "description": "Agent worker on prod-server-1",
  "expires_at": null
}
```

**Response 201** — Key shown ONCE; not retrievable again.
```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440002",
    "name": "agent-worker-prod-1",
    "key": "sc_live_k8j3h2n1...",
    "prefix": "sc_live_k8j3h...",
    "created_at": "2026-03-25T10:00:00Z"
  }
}
```

---

### `DELETE /api/v1/auth/api-keys/{key_id}`
Revoke an API key immediately.

**Response 200**
```json
{ "success": true, "data": { "revoked": true, "key_id": "..." } }
```

---

## Data Model

### `users`
```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) UNIQUE NOT NULL,
    name            VARCHAR(100) NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,   -- bcrypt, rounds=12
    role            VARCHAR(20) NOT NULL DEFAULT 'operator',
    is_active       BOOLEAN DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    failed_attempts INT DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_users_email ON users(email);
```

### `api_keys`
```sql
CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(100) NOT NULL,
    key_prefix      VARCHAR(20) NOT NULL,     -- first 12 chars for display
    hashed_key      VARCHAR(255) NOT NULL,    -- bcrypt(full_key)
    role            VARCHAR(20) NOT NULL DEFAULT 'service',
    description     TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    expires_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_api_keys_active ON api_keys(is_active) WHERE is_active = TRUE;
```

### `refresh_tokens`
```sql
CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  VARCHAR(255) NOT NULL UNIQUE,  -- SHA-256 of raw token
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    ip_address  INET,
    user_agent  TEXT
);
CREATE INDEX idx_refresh_tokens_user ON refresh_tokens(user_id);
CREATE INDEX idx_refresh_tokens_expiry ON refresh_tokens(expires_at);
```

---

## Business Logic

### Login Flow
1. Lookup user by email; if not found, return 401 (same error as wrong password — no enumeration).
2. Check `is_active = TRUE`; if false, return 403 `ACCOUNT_DISABLED`.
3. Check `locked_until`; if in future, return 429 `ACCOUNT_LOCKED` with `retry_after_seconds`.
4. Verify `bcrypt.checkpw(password, hashed_password)`.
5. On failure: increment `failed_attempts`; if ≥ 5, set `locked_until = NOW() + 15 minutes`.
6. On success: reset `failed_attempts = 0`, update `last_login_at`.
7. Generate access JWT: `{sub: user_id, role, exp: +900s, jti: uuid}`.
8. Generate refresh token: cryptographically random 64-byte hex string.
9. Store `SHA-256(refresh_token)` in `refresh_tokens` table with `expires_at = NOW() + 7 days`.
10. Write `audit_logs` entry `action=LOGIN`.

### API Key Authentication Flow
1. Extract key from `X-Api-Key` header.
2. Extract prefix (first 12 chars).
3. Query `api_keys` where `key_prefix = prefix AND is_active = TRUE`.
4. `bcrypt.checkpw(full_key, hashed_key)`.
5. If expired (`expires_at < NOW()`), return 401 `KEY_EXPIRED`.
6. Update `last_used_at` asynchronously (fire-and-forget, non-blocking).
7. Attach `role=service` to request context.

### LiveKit Token Generation
1. Validate caller API key (must have `role=service`).
2. Verify `call_sid` exists in `call_sessions` table with `started_at` within last 4 hours.
3. Build LiveKit `AccessToken` with requested grants.
4. Sign with `LIVEKIT_API_SECRET` env var.
5. Set TTL = min(`ttl_seconds`, 7200) — hard cap 2 hours.
6. Return signed JWT string.

---

## Edge Cases

| Scenario | Handling |
|---|---|
| Refresh token already revoked | Return 401 `TOKEN_REVOKED`; log potential token theft; optionally revoke all tokens for user |
| LiveKit token requested for unknown `call_sid` | Return 404 `CALL_SESSION_NOT_FOUND` |
| bcrypt comparison takes > 500ms | Accept (intentional); add timeout of 2s to prevent stall |
| Concurrent login from same account | Allowed; multiple refresh tokens valid simultaneously (different devices) |
| API key used after revocation | Immediate 401; no grace period |
| JWT with invalid signature | Return 401 `INVALID_TOKEN`; do not log full token (only jti) |
| Clock skew between services | Allow ±30 second leeway on JWT `exp` validation |

---

## Constraints

- JWT signing algorithm: `HS256` for internal tokens; LiveKit tokens use `HS256` per LiveKit SDK.
- Secret key for JWT: minimum 256-bit random value; stored in `JWT_SECRET` env var.
- bcrypt rounds: 12 (adjust up if hardware allows, never below 10).
- Token blocklist: stored in Redis with TTL = remaining token lifetime. Key: `blocklist:jti:{jti}`.
- Rate limiting: implemented as Redis sliding window counter. Key: `ratelimit:login:{ip}`.
- All password comparisons use constant-time comparison to prevent timing attacks.
- No user enumeration: identical error for wrong email vs wrong password.

---

## Acceptance Criteria

- [ ] Valid login returns access + refresh token; access token decodes to correct `sub` and `role`.
- [ ] Expired access token returns 401 on any protected endpoint.
- [ ] Refresh with revoked token returns 401 and does not issue new tokens.
- [ ] 5 failed logins within 15 minutes locks the account; 6th attempt returns 429 with `retry_after_seconds`.
- [ ] API key deleted via `DELETE /api-keys/{id}` is rejected within 1 second on next use.
- [ ] LiveKit token generated for valid `call_sid` is accepted by LiveKit Cloud.
- [ ] LiveKit token requested with invalid API key returns 401.
- [ ] `audit_logs` contains an entry for every login, logout, and key creation event.
- [ ] Password with fewer than 12 characters rejected with 400 `WEAK_PASSWORD`.
- [ ] All auth endpoints return identical latency (± 50ms) for valid and invalid credentials (timing-safe).
