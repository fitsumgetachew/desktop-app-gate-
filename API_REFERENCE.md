# Mock Remote Server — API Reference

This document is the primary integration reference for the desktop app developer.
It covers every endpoint, request/response shape, enum values, error codes, and behavioral rules.

**Server version:** 2.0.0
**Base URL (local dev):** `http://localhost:8000`
**Interactive docs:** `GET /docs` · `GET /openapi.json`

---

## Conventions

### Authentication

All protected endpoints require:

```
Authorization: Bearer <access_token>
```

### Time

All time fields are Unix timestamps in **seconds** (integer).

### User identity

The `user` object in all auth responses uses a `uuid` string field (not an integer id).

### Enums — strict values only

The server **rejects** any value not in this list with `422 Unprocessable Entity`.

| Field | Valid values |
|---|---|
| `direction` | `ENTRY` \| `EXIT` |
| `decision` | `ALLOW` \| `DENY` \| `NEED_MANUAL` |
| `decision_source` | `AUTO` \| `MANUAL` |
| `permit_status` | `ALLOWED` \| `DENIED` \| `BLACKLISTED` \| `EXPIRED` |

### Error format

```json
{ "detail": "Human-readable message" }
```

Validation errors return an array:

```json
{ "detail": [ { "loc": ["body", "direction"], "msg": "Input should be 'ENTRY' or 'EXIT'", "type": "literal_error" } ] }
```

---

## Desktop App Startup Flow

This is the recommended sequence the desktop app follows on every launch:

```
1. POST /auth/desktop/start      → get one-time code
2. POST /auth/desktop/exchange   → exchange code for access + refresh tokens
3. POST /devices/check           → confirm device is registered and get gate/lane assignment
4. GET  /sync/allowlist          → pull full or incremental allowlist
5. GET  /sync/manual-reasons     → pull manual reason list
   (loop: submit events via POST /events or POST /events/batch)
6. POST /auth/refresh            → renew access token before expiry (every ~15 min)
```

---

## Authentication

### POST /auth/desktop/start

**No auth required.**

First step of the desktop login flow. In production, this endpoint is called by the
web portal after the user has authenticated via Firebase/SSO — credentials never
leave the portal. In the mock, email + password are accepted directly because there
is no portal in the dev environment. The two-step structure is preserved so the
desktop client code does not need to change when connected to the real backend.

**Request**

```json
{
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "email": "guard@university.edu",
  "password": "Guard123!"
}
```

**Response 200**

```json
{
  "code": "VkIN-eebiX_Op3KB7NTbyMnlCf8PpQBY",
  "expires_in": 120
}
```

The `code` is a single-use token valid for **120 seconds**. Pass it immediately to
`/auth/desktop/exchange`. It cannot be reused.

**Errors**

- `401` — Invalid credentials

---

### POST /auth/desktop/exchange

**No auth required.**

Second step. The desktop exchanges the one-time code for a long-lived access token
and a refresh token.

**Request**

```json
{
  "code": "VkIN-eebiX_Op3KB7NTbyMnlCf8PpQBY",
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77"
}
```

The `device_id` must match the one used in `/auth/desktop/start`.

**Response 200**

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<opaque-token>",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "uuid": "00000000-0000-0000-0000-000000000002",
    "email": "guard@university.edu",
    "full_name": "Gate Guard",
    "role": "guard"
  }
}
```

- `access_token` expires in **900 seconds** (15 minutes). Use it for all API calls.
- `refresh_token` is opaque and long-lived (30 days). Store it securely. Use it with
  `/auth/refresh` to get a new access token without re-logging in.

**Errors**

- `401` — Invalid or expired code
- `401` — Code was not issued for this device

---

### POST /auth/refresh

**No auth required.**

Exchange a valid refresh token for a new access token. Call this before the current
access token expires to maintain a session without re-authenticating.

**Request**

```json
{
  "refresh_token": "<opaque-token>"
}
```

**Response 200**

```json
{
  "access_token": "<new-jwt>",
  "token_type": "bearer",
  "expires_in": 900
}
```

**Errors**

- `401` — Invalid refresh token (expired, revoked, or not found)

---

### POST /auth/login

**No auth required. For admin tooling and testing only.**

Direct email/password login. Returns the same token shape as the desktop flow.
Desktop apps should use `/auth/desktop/start` + `/auth/desktop/exchange` instead.

**Request**

```json
{
  "email": "admin@university.edu",
  "password": "Admin123!"
}
```

**Response 200**

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<opaque-token>",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "uuid": "00000000-0000-0000-0000-000000000001",
    "email": "admin@university.edu",
    "full_name": "System Admin",
    "role": "admin"
  }
}
```

**Errors**

- `401` — Invalid credentials

---

## Devices

### POST /devices/register

**Protected.**

Register a new device or update an existing one. Safe to call on every app launch
as it is fully idempotent — re-registering an existing `device_id` updates its
record without creating a duplicate.

**Request**

```json
{
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "device_name": "Gate-1-Lane-A",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "gate_id": "GATE-1",
  "gate_name": "Main Gate",
  "lane_id": "LANE-A",
  "lane_name": "Entry Lane"
}
```

`gate_name` and `lane_name` are optional display labels. `gate_id` and `lane_id` are
the authoritative identifiers.

**Response 200**

```json
{
  "ok": true,
  "device": {
    "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
    "device_name": "Gate-1-Lane-A",
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "gate_id": "GATE-1",
    "lane_id": "LANE-A",
    "status": "ACTIVE",
    "app_version": null,
    "last_error": null,
    "last_seen_at": 1730000100,
    "registered_at": 1730000100
  }
}
```

---

### POST /devices/check

**Protected.**

Confirm whether a device is registered and retrieve its gate/lane assignment.
Call this after login to validate the device before pulling the allowlist or
submitting events. Use the returned gate/lane IDs when constructing event payloads.

**Request**

```json
{
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77"
}
```

**Response 200 — registered**

```json
{
  "registered": true,
  "server_time": 1730000200,
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "device_name": "Gate-1-Lane-A",
  "status": "ACTIVE",
  "gate": { "id": "GATE-1", "name": "Main Gate" },
  "lane": { "id": "LANE-A", "name": "Entry Lane" }
}
```

**Response 200 — not registered**

```json
{
  "registered": false,
  "server_time": 1730000200,
  "message": "Device not registered. Use POST /devices/register first."
}
```

This always returns HTTP 200. Check the `registered` flag, not the status code.

---

### POST /devices/heartbeat

**Protected.**

Update device health status. Call periodically (e.g. every 5 minutes) while the
app is running.

**Request**

```json
{
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "app_version": "1.2.0",
  "status": "OK",
  "last_error": null
}
```

`status` is free text. Suggested values: `OK`, `ERROR`, `ACTIVE`.

**Response 200**

```json
{
  "ok": true,
  "server_time": 1730000200
}
```

**Errors**

- `404` — Device not registered

---

## Sync

### GET /sync/allowlist

**Protected.**

Pull the vehicle allowlist. Pass `since_version` for incremental updates — the
server returns only items updated at or after that timestamp.

**Query parameters**

| Param | Type | Description |
|---|---|---|
| `since_version` | string (optional) | Unix timestamp. Omit for a full pull. |

**Response 200**

```json
{
  "version": "1730000300",
  "items": [
    {
      "plate_number": "ABC-1234",
      "status": "ALLOWED",
      "valid_to": null,
      "owner_name": "Dr. Ada Lovelace",
      "updated_at": 1730000000
    },
    {
      "plate_number": "XYZ-9876",
      "status": "DENIED",
      "valid_to": null,
      "owner_name": "Unregistered Vehicle",
      "updated_at": 1730000000
    },
    {
      "plate_number": "BLK-6666",
      "status": "BLACKLISTED",
      "valid_to": null,
      "owner_name": "Security Hold",
      "updated_at": 1730000000
    }
  ]
}
```

**Incremental sync pattern**

1. First pull: omit `since_version`, store the returned `version` locally.
2. Subsequent pulls: send `since_version=<stored_version>`.
3. After each pull, update the stored `version` to the new value.

**Status values in sync items**

The server evaluates `valid_to` at response time. A plate stored as `ALLOWED` with
a past `valid_to` will be returned as `EXPIRED`. Always trust the returned `status`,
not just the raw stored value.

**Errors**

- `400` — `since_version` is not a valid integer

---

### GET /sync/manual-reasons

**Protected.**

Pull the list of manual override reasons. Refresh this list on each login or on a
long pull interval. Only show items where `is_active: true` in the UI.

**Response 200**

```json
{
  "items": [
    { "id": 1, "reason_text": "OCR failed",       "is_active": true },
    { "id": 2, "reason_text": "Camera blur",       "is_active": true },
    { "id": 3, "reason_text": "VIP call",          "is_active": true },
    { "id": 4, "reason_text": "Temporary permit",  "is_active": true },
    { "id": 5, "reason_text": "Manual override",   "is_active": true }
  ]
}
```

Use the `id` when submitting events with `manual_reason_id`.

---

## Events

### POST /events

**Protected.**

Submit a single gate access event. This endpoint is **idempotent** — submitting
the same `id` twice is safe and returns the original response with `deduped: true`.
Always generate the `id` on the desktop side before the event occurs, so retries
after network failure never create duplicate records.

**Request**

```json
{
  "id": "7bcb1c5f-4bb1-4f7f-8c6b-94b5b1e3f9a0",
  "event_time": 1730000000,
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "gate_id": "GATE-1",
  "lane_id": "LANE-A",
  "direction": "ENTRY",
  "plate_number_raw": "abc 1234",
  "plate_number_final": "ABC-1234",
  "confidence": 0.93,
  "decision": "ALLOW",
  "decision_source": "AUTO",
  "manual_by_user_id": null,
  "manual_by_username": null,
  "manual_reason_id": null,
  "manual_reason_text": null,
  "manual_note": null,
  "is_offline_event": false,
  "evidence_uploaded_url": null
}
```

**Field notes**

| Field | Notes |
|---|---|
| `id` | UUID generated by the desktop **before** the event. This is the idempotency key. Required. |
| `event_time` | When the gate event actually happened (not when it was synced). |
| `plate_number_raw` | Exactly as read by OCR, before normalisation. |
| `plate_number_final` | The plate used for the decision. Stored as uppercase by the server. |
| `confidence` | OCR confidence 0.0–1.0. Pass `null` for manual decisions; server stores `0.0`. |
| `manual_by_user_id` | UUID of the guard who overrode. Required when `decision_source = MANUAL`. |
| `manual_by_username` | Display name of the guard. Optional fallback for logs/display. |
| `manual_reason_id` | Integer `id` from `/sync/manual-reasons`. Required when `decision_source = MANUAL`. |
| `manual_reason_text` | Text of the reason. Optional fallback for display. |
| `is_offline_event` | `true` if this event was recorded while offline and is being synced later. |
| `evidence_uploaded_url` | URL of uploaded evidence image (after a separate upload step). `null` if not yet uploaded. |

**Response 200 — new event**

```json
{
  "ok": true,
  "event_id": "7bcb1c5f-4bb1-4f7f-8c6b-94b5b1e3f9a0",
  "received_at": 1730000050,
  "deduped": false,
  "presence_update": {
    "plate_number": "ABC-1234",
    "inside_status": "INSIDE",
    "updated_at": 1730000050
  }
}
```

**Response 200 — duplicate (safe retry)**

```json
{
  "ok": true,
  "event_id": "7bcb1c5f-4bb1-4f7f-8c6b-94b5b1e3f9a0",
  "received_at": 1730000050,
  "deduped": true,
  "presence_update": null
}
```

**Presence logic**

| Decision | Direction | Presence result |
|---|---|---|
| `ALLOW` | `ENTRY` | `INSIDE` |
| `ALLOW` | `EXIT` | `OUTSIDE` |
| `DENY` | any | no change |
| `NEED_MANUAL` | any | no change |

**Offline ordering rule:** the server only updates presence if `event_time` is newer
than the currently stored state. Late-arriving offline events never overwrite a more
recent presence state.

---

### POST /events/batch

**Protected.**

Submit multiple events in one request. Designed for offline sync — send the full
local queue in a single call. Each event is processed independently. A bad item does
not block the others. Check `results[n].ok` for per-item status.

**Request**

```json
{
  "items": [
    {
      "id": "aaa-...",
      "event_time": 1730000000,
      "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
      "gate_id": "GATE-1",
      "lane_id": "LANE-A",
      "direction": "ENTRY",
      "plate_number_raw": "ABC-1234",
      "plate_number_final": "ABC-1234",
      "confidence": 0.91,
      "decision": "ALLOW",
      "decision_source": "AUTO",
      "is_offline_event": true
    },
    {
      "id": "bbb-...",
      "event_time": 1730000120,
      "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
      "gate_id": "GATE-1",
      "lane_id": "LANE-A",
      "direction": "EXIT",
      "plate_number_raw": "XYZ-9876",
      "plate_number_final": "XYZ-9876",
      "confidence": 0.88,
      "decision": "DENY",
      "decision_source": "AUTO",
      "is_offline_event": true
    }
  ]
}
```

All fields per item are the same as `POST /events`.

**Response 200**

```json
{
  "ok": true,
  "received_at": 1730000200,
  "results": [
    {
      "ok": true,
      "event_id": "aaa-...",
      "received_at": 1730000200,
      "deduped": false,
      "presence_update": {
        "plate_number": "ABC-1234",
        "inside_status": "INSIDE",
        "updated_at": 1730000200
      },
      "error": null
    },
    {
      "ok": true,
      "event_id": "bbb-...",
      "received_at": 1730000200,
      "deduped": false,
      "presence_update": null,
      "error": null
    }
  ]
}
```

The outer `ok: true` means the batch request itself was accepted. Individual items
that fail will have `ok: false` and a message in `error`. Deduplicated items will
have `deduped: true`.

---

## Vehicles

### GET /vehicles/lookup/{plate}

**Protected.**

Look up a single plate in real time. Use this when OCR produces a plate not found
in the local allowlist cache, or when you need to confirm current status directly
from the server.

**Path parameter:** plate number (case-insensitive, normalised to uppercase).

**Response 200**

```json
{
  "plate_number": "ABC-1234",
  "status": "ALLOWED",
  "valid_to": null,
  "owner_name": "Dr. Ada Lovelace"
}
```

**Expiry enforcement:** if `status` is `ALLOWED` and `valid_to` is in the past,
the server returns `EXPIRED` — not `ALLOWED`. Do not grant access to `EXPIRED` permits.

**Errors**

- `404` — Plate not found in database

---

### POST /vehicles/register

**Protected. Admin role required.**

Register a new vehicle or update an existing one. Only accounts with `role: admin`
can call this endpoint.

**Request**

```json
{
  "plate_number": "NEW-2222",
  "owner_name": "Test Owner",
  "permit_status": "ALLOWED",
  "valid_to": 1735000000
}
```

`valid_to` is optional. Set to `null` for permits with no expiry.

**Response 200**

```json
{
  "ok": true,
  "vehicle": {
    "plate_number": "NEW-2222",
    "status": "ALLOWED",
    "valid_to": 1735000000,
    "owner_name": "Test Owner"
  }
}
```

**Errors**

- `403` — Admin access required

---

## Seed Data

### Default users

| Email | Password | Role | UUID |
|---|---|---|---|
| `admin@university.edu` | `Admin123!` | `admin` | `00000000-0000-0000-0000-000000000001` |
| `guard@university.edu` | `Guard123!` | `guard` | `00000000-0000-0000-0000-000000000002` |

### Default allowlist

| Plate | Status | Owner |
|---|---|---|
| `ABC-1234` | `ALLOWED` | Dr. Ada Lovelace |
| `XYZ-9876` | `DENIED` | Unregistered Vehicle |
| `VIP-0001` | `ALLOWED` | Chancellor |
| `BLK-6666` | `BLACKLISTED` | Security Hold |

### Default manual reasons

| ID | Text |
|---|---|
| 1 | OCR failed |
| 2 | Camera blur |
| 3 | VIP call |
| 4 | Temporary permit |
| 5 | Manual override |

---

## Offline-First Guidelines

1. **Generate event UUIDs locally, before the event occurs.** This allows safe retry
   without duplication — the server will recognise and silently ignore the duplicate.

2. **Store events locally first, sync second.** Set `is_offline_event: true` for any
   event submitted after reconnection.

3. **Preserve original `event_time`.** Always use the time the gate event happened,
   not the time it was synced. The server uses `event_time` for presence ordering.

4. **Use `/events/batch` for sync.** Submit the full local queue in one request.
   Process the per-item results to determine which events need retry.

5. **Do not retry a failed batch item with a new UUID.** Keep the original UUID so
   the server can deduplicate if it was partially committed.

---

## Token Management

| Token | TTL | Storage |
|---|---|---|
| Access token (JWT) | 15 minutes | In-memory only — do not persist |
| Refresh token (opaque) | 30 days | Secure local storage (keychain / encrypted file) |
| Desktop one-time code | 2 minutes | In-memory — exchange immediately |

**Recommended strategy:** start a background timer. When the access token has
~60 seconds left, call `/auth/refresh` proactively. If the app restarts and the
access token has expired, use the stored refresh token to get a new one without
requiring the user to log in again.
