# Mock Remote Server API Reference

This document describes all API endpoints, request/response structures, examples, and common errors. It is designed for desktop app integration and mirrors the mock server behavior.

Base URL (local)

`http://localhost:8000`

OpenAPI

- `GET /docs`
- `GET /openapi.json`

## Conventions

Auth

- All protected endpoints require `Authorization: Bearer <access_token>`.

Time

- All time fields are Unix timestamps in seconds.

Enums

- `direction`: `ENTRY` | `EXIT`
- `decision`: `ALLOW` | `DENY` | `NEED_MANUAL`
- `decision_source`: `AUTO` | `MANUAL`
- `permit_status`: `ALLOWED` | `DENIED` | `BLACKLISTED`
- `device status`: free text, commonly `ACTIVE`, `OK`, `ERROR`

Error format

- Standard FastAPI errors: `{ "detail": "..." }`

## Authentication

### POST /auth/login

Request body

```json
{
  "email": "admin@university.edu",
  "password": "Admin123!"
}
```

Response 200

```json
{
  "access_token": "<jwt-like-token>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": 1,
    "email": "admin@university.edu",
    "full_name": "System Admin",
    "role": "admin"
  }
}
```

Errors

- 401: `{ "detail": "Invalid credentials" }`

## Devices

### POST /devices/register

Protected

Headers

- `Authorization: Bearer <token>`

Request body

```json
{
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "device_name": "Gate-1-Lane-A",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "gate_id": "GATE-1",
  "lane_id": "LANE-A"
}
```

Response 200

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
  },
  "device_token": null
}
```

Notes

- Registration is allowed directly in the mock server.

### POST /devices/heartbeat

Protected

Headers

- `Authorization: Bearer <token>`

Request body

```json
{
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "app_version": "1.2.0",
  "status": "OK",
  "last_error": null
}
```

Response 200

```json
{
  "ok": true,
  "server_time": 1730000200
}
```

Errors

- 404: `{ "detail": "Device not registered" }`

## Allowlist Sync

### GET /sync/allowlist

Protected

Headers

- `Authorization: Bearer <token>`

Query params

- `since_version` optional, Unix timestamp. Returns items updated after this timestamp.

Response 200

```json
{
  "version": "1730000300",
  "items": [
    {
      "plate_number": "ABC-1234",
      "status": "ALLOWED",
      "valid_to": null,
      "updated_at": 1730000000
    },
    {
      "plate_number": "BLK-6666",
      "status": "BLACKLISTED",
      "valid_to": null,
      "updated_at": 1730000000
    }
  ]
}
```

Errors

- 400: `{ "detail": "Invalid since_version" }`

## Manual Reasons

### GET /sync/manual-reasons

Protected

Headers

- `Authorization: Bearer <token>`

Response 200

```json
{
  "items": [
    { "id": 1, "reason_text": "OCR failed", "is_active": true },
    { "id": 2, "reason_text": "Camera blur", "is_active": true }
  ]
}
```

## Events

### POST /events

Protected

Headers

- `Authorization: Bearer <token>`

Request body

```json
{
  "event_time": 1730000000,
  "device_id": "5a4d2f5a-8f68-4c65-9f94-4a09e6d53f77",
  "gate_id": "GATE-1",
  "lane_id": "LANE-A",
  "direction": "ENTRY",
  "plate_number_raw": "ABC-1234",
  "plate_number_final": "ABC-1234",
  "confidence": 0.93,
  "decision": "ALLOW",
  "decision_source": "AUTO",
  "manual_by": null,
  "manual_reason": null,
  "manual_note": null,
  "is_offline_event": false,
  "evidence_local_path": "/data/captures/abc-1234.jpg",
  "evidence_uploaded_url": null
}
```

Response 200

```json
{
  "ok": true,
  "event_id": "7bcb1c5f-4bb1-4f7f-8c6b-94b5b1e3f9a0",
  "presence_update": {
    "plate_number": "ABC-1234",
    "state": "INSIDE",
    "updated_at": 1730000000
  }
}
```

Presence logic

- `ALLOW + ENTRY` sets `INSIDE`.
- `ALLOW + EXIT` sets `OUTSIDE`.
- Any other decision does not change presence.

## Vehicles

### GET /vehicles/lookup/{plate}

Protected

Headers

- `Authorization: Bearer <token>`

Response 200

```json
{
  "plate_number": "ABC-1234",
  "status": "ALLOWED",
  "valid_to": null,
  "owner_name": "Dr. Ada Lovelace"
}
```

Errors

- 404: `{ "detail": "Plate not found" }`

### POST /vehicles/register

Protected (admin only)

Headers

- `Authorization: Bearer <token>`

Request body

```json
{
  "plate_number": "NEW-2222",
  "owner_name": "Test Owner",
  "permit_status": "ALLOWED",
  "valid_to": 1735000000
}
```

Response 200

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

Errors

- 403: `{ "detail": "Admin access required" }`

## Seed Data

Default users

- `admin@university.edu` / `Admin123!`
- `guard@university.edu` / `Guard123!`

Default allowlist examples

- `ABC-1234` ALLOWED
- `XYZ-9876` DENIED
- `VIP-0001` ALLOWED
- `BLK-6666` BLACKLISTED

Default manual reasons

- OCR failed
- Camera blur
- VIP call
- Temporary permit
- Manual override
