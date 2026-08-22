# Smart Gate — Desktop ↔ Server API Integration Contract

**Version 2.2 — finalized 2026-08-12.** Extracted from the working code of both repos
(desktop app `smart_gate`, reference server `SIT-gate-desktopapp`). This is the
contract the production portal server must implement so the desktop gate
application works against it unchanged. The reference (mock) server passes the
full desktop test suite against this exact contract; treat it as the executable
specification.

- Desktop app: PySide6, reports `app_version: "2.0.0"` in heartbeats.
- Reference server: FastAPI, version 2.2.0, at `~/Software-Projects/SIT/SIT-gate-desktopapp`.
- Base URL is configurable (`API_BASE_URL`, default `http://localhost:8000`); every
  endpoint path is individually overridable via env keys, so the portal may use
  different paths if needed — but defaults below are recommended.

---

## 1. Design principles the server must honor

1. **Offline-first.** The desktop never blocks a gate decision on the network. All
   decisions are written to a local queue and pushed later; the server must treat
   `POST /events/batch` as an idempotent, eventually-consistent ingest.
2. **Idempotency by client UUID.** Every event carries a client-generated UUID4
   `id`. Re-submission of the same `id` must succeed with `deduped: true` and the
   original `received_at`, and must not double-apply side effects.
3. **Canonical plates.** All matching uses the canonical form: uppercase, strip
   every character outside `A–Z0–9` (`abc-1234` → `ABC1234`). The server stores and
   returns `plate_number` canonically; the human-entered original may be echoed in
   `display_plate` (display only, never matched).
4. **Bearer auth.** All endpoints except the three auth entry points require
   `Authorization: Bearer <access_token>`. Access tokens are short-lived (900 s);
   the desktop refreshes proactively at 80% of TTL and reactively on 401
   (single retry). Send a real `expires_in` — the client schedules refresh from it.
5. **Roles.** `admin` (portal staff), `guard` (gate operator). The desktop operates
   as `guard`; admin-only endpoints exist for the portal UI / back office.

---

## 2. Authentication

### 2.1 Desktop two-step login

The two-step shape exists so the production portal can insert its own identity
front-end (SSO/Firebase): step 1 is where the portal authenticates the user and
mints a one-time code; step 2 is the desktop exchanging that code for tokens.
The reference server accepts email+password directly in step 1.

Which half of step 1 the desktop performs is the `AUTH_MODE` setting:

| `AUTH_MODE` | Step 1 | Step 2 |
|---|---|---|
| `mock` (default) | desktop posts `/auth/desktop/start` with email+password | desktop posts `/auth/desktop/exchange` |
| `portal` | operator signs in at `PORTAL_SSO_URL?client=smart-gate&device_id=…` in a browser; the portal mints the code | desktop posts `/auth/desktop/exchange` |

In `portal` mode no credential ever reaches the desktop — it only ever sends the
code. Everything after the exchange (token persistence, `/devices/register`,
`/devices/check`) is the same code path in both modes.

**POST `/auth/desktop/start`** — no auth.
Request: `{ "device_id": str, "email": str, "password": str }`
Response 200: `{ "code": str, "expires_in": int }` — one-time code, 120 s TTL,
single use, bound to `device_id`. 401 on bad credentials.

**POST `/auth/desktop/exchange`** — no auth.
Request: `{ "code": str, "device_id": str }`
Response 200 (`TokenResponse`):

```json
{
  "access_token": "<JWT>",
  "refresh_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 900,
  "user": { "uuid": "…", "email": "…", "full_name": "…", "role": "guard" }
}
```

`user.uuid` and `user.email` are **required** (the desktop stamps them onto manual
decisions). 401 when the code is unknown, already used, expired, or was issued
for a different `device_id`.

**POST `/auth/refresh`** — no auth.
Request: `{ "refresh_token": str }`
Response 200: `{ "access_token": str, "token_type": "bearer", "expires_in": int }`.
Refresh tokens live 30 days. Rotation is optional: if the response includes a new
`refresh_token`, the desktop persists it; the reference server does not rotate,
**the portal rotates on every call** and the old token dies immediately.
401 for unknown/revoked/expired tokens → the desktop forces re-login.

Because rotation makes a replayed refresh token a security event (reuse
detection can revoke the whole token family), the desktop serialises refreshes
process-wide: only one `/auth/refresh` is ever in flight, and threads that were
about to issue a second one reuse the first one's result.

**POST `/auth/logout`** — no auth. Portal only; the reference server does not
implement it. Request: `{ "refresh_token": str }` — revokes the session
server-side. The desktop calls it before dropping its local session and ignores
any failure, so logout works with the network down.

**POST `/auth/login`** — legacy direct login, kept for admin tooling/tests. The
desktop app never calls it.

Access-token claims (reference implementation, HS256): `sub` (user uuid),
`email`, `role`, `device_id`, `iat`, `exp`, `iss`, `aud`, `type: "access"`.

### 2.2 Desktop behavior the server can rely on

- Proactive refresh at 80% of `expires_in`; access token held in memory only.
- On any 401 from a business endpoint: one refresh, one retry, then the session
  is declared expired and the guard is sent back to login.
- At most one `/auth/refresh` in flight per process, across every worker thread.
- Logout: local in `mock` mode (tokens discarded); in `portal` mode it first
  posts `/auth/logout` (≤ 5 s, failures ignored) and then discards them.

---

## 3. Devices

A desktop installation self-identifies with a generated UUID4 `device_id` and its
MAC address. Immediately after login it registers and then validates itself:

**POST `/devices/register`** — any authenticated user.
Request: `{ "device_id": str, "device_name": str, "mac_address": str, "gate_id": str, "lane_id": str }`
(optional: `gate_name`, `lane_name`). Upsert by `device_id`.
Response 200: `{ "ok": true, "device": { device_id, device_name, mac_address, gate_id, lane_id, status, app_version, last_error, last_seen_at, registered_at } }`.
Registration failure is non-fatal to login (logged, login continues to check).

**POST `/devices/check`** — any authenticated user.
Request: `{ "device_id": str }`
Response 200 — always 200, registration state is in the body:

```json
{ "registered": true, "server_time": 1765500000,
  "device_id": "…", "device_name": "…", "status": "ACTIVE",
  "gate": { "id": "GATE-1", "name": "Main Gate" },
  "lane": { "id": "LANE-A", "name": "Lane A" } }
```

or `{ "registered": false, "server_time": …, "message": "Device not registered. …" }`.

**Desktop expectations — important:**
- `registered: false` → login is **blocked** (fail-closed) and tokens are cleared.
- Network error on check → the app proceeds in explicit *offline mode* (banner).
- A server that does not implement the endpoint at all (404/501) is treated the
  same way: offline mode, with the banner saying the check is unavailable rather
  than blaming the network.
- **403 is different**: the portal answers 403 when a session's `device_id` does
  not match the device its credentials were issued for. That is fixable by the
  operator, so the desktop fails closed with "These credentials were issued for a
  different device. Sign in again from this machine." rather than sliding into
  offline mode. A 403 from any device endpoint during sync likewise forces
  re-login instead of an endless backoff.
- `gate.id` / `lane.id` are **authoritative**: the desktop overwrites its local
  `GATE_ID`/`LANE_ID` config with them and stamps them on all subsequent events.
  Assigning gates/lanes to devices server-side is therefore the mechanism for
  centrally managing multiple gates.

**POST `/devices/heartbeat`** — any authenticated user.
Request: `{ "device_id": str, "app_version": "2.0.0", "status": "OK" | "DEGRADED", "last_error": str | null }`
Response 200: `{ "ok": true, "server_time": int }`. 404 if the device is unknown.
Sent every 5th sync cycle (≈ every 50 s at the default 10 s interval), **and
immediately whenever `status` changes**, so a gate that has just broken (or just
recovered) is not silent for the rest of the window. Failures are swallowed
client-side and retried on the next cycle.

`status` is the health of the last sync cycle, not the device's provisioning
state: `DEGRADED` means the gate is alive and operating but some step failed
(e.g. the allowlist pull), with `last_error` giving a short reason such as
`"allowlist sync failed: HTTP 404"`. `last_error` is deliberately limited to a
step name plus an HTTP status or exception class — never response bodies, URLs,
tokens or plate data. **Servers must keep this client-reported health separate
from their own device lifecycle status** (the portal stores it as
`reported_status`) so a heartbeat can never overwrite a provisioning decision.

Critically, a failing sync step no longer suppresses the heartbeat: the desktop
finishes the cycle, reports the failure, and only then raises internally. Without
this a broken sync makes the gate look unplugged, which is exactly backwards —
the failure most worth seeing produced the least information.

---

## 4. Allowlist synchronization

**GET `/sync/allowlist?since_version=<int>`** — any authenticated user.

- `since_version` absent/`<=0` → **full sync**: all rows, `deleted: []`. The
  desktop **replaces** its whole cache with the result.
- `since_version > 0` → **delta**: rows with `updated_at >= since_version`
  (inclusive) plus `deleted`: canonical plates whose tombstone `deleted_at >=
  since_version`. The desktop upserts `items` and purges `deleted`.
- Response `version` is a **string** holding an integer (the max `updated_at` /
  `deleted_at` watermark). The desktop stores it per-row and sends back
  `MAX(version)` as the next `since_version`. Monotonically non-decreasing
  integers (epoch seconds work) are required.
- Re-registering a deleted plate must clear its tombstone (so it stops appearing
  in `deleted` and reappears in `items`).

Each item is a `VehicleDetails` + `updated_at`:

```json
{ "plate_number": "ABC1234", "status": "ALLOWED", "valid_to": null,
  "valid_from": null, "alert": false, "owner_name": "Abebe Kebede",
  "owner_first_name": "Abebe", "owner_last_name": "Kebede",
  "relationship": "STAFF", "department": "Engineering", "phone": "+251…",
  "vehicle_make": "Toyota", "vehicle_model": "Corolla", "vehicle_color": "White",
  "display_plate": "ABC-1234", "note": null, "updated_at": 1765500000 }
```

**GET `/sync/manual-reasons`** — any authenticated user.
Response: `{ "items": [ { "id": int, "reason_text": str, "is_active": bool } ] }`.
The desktop caches the full list (used for manual-override reasons and temp
permits) and resolves `reason_id` from `reason_text` locally.

### Sync cycle (what the server will observe)

Every `SYNC_INTERVAL_SECONDS` (default 10, min 5), or immediately after a gate
decision, the desktop runs in order: allowlist delta → manual reasons (soft-fail)
→ `POST /events/batch` (≤ 50 events) → evidence uploads (≤ 20) → heartbeat (every
5th cycle). On network failure it backs off exponentially 2→4→8→16→32→60 s and
resets to the normal interval on success.

---

## 5. Statuses and the gate decision (traffic light)

**Stored statuses** (the only values ever written): `ALLOWED | DENIED | BLACKLISTED`.
**Derived statuses** (computed at response time, never stored):
`EXPIRED` (`status=ALLOWED` and `valid_to < now`),
`NOT_YET_VALID` (`status=ALLOWED` and `valid_from > now`).
`BLACKLISTED` is terminal — no date logic ever changes it.
**`alert`**: must be `true` iff effective status is `BLACKLISTED` — it is the
client's trigger for the audible alarm.
**`relationship`**: `STAFF | STUDENT | CONTRACTOR | VISITOR | VIP | OTHER`.

Desktop mapping (camera view state):

| Cache/lookup result | UI state | Behavior |
|---|---|---|
| `ALLOWED`, within validity | **GREEN** | countdown (default 5 s) → auto-ALLOW (`decision_source: "AUTO"`); guard can cancel |
| `BLACKLISTED` or `alert: true` | **RED + siren** | never auto; override requires reason + note + confirmation |
| `DENIED` | **RED**, silent | manual only |
| expired / not-yet-valid (derived from `valid_to`/`valid_from`) | **ORANGE** | offers registration |
| unknown plate (no cache row, lookup 404) | **ORANGE** | offers visitor registration |

⚠️ **Server-implementer note:** the desktop derives expiry states from
`valid_to`/`valid_from` itself; always include those fields. Do not rely on
sending the literal string `NOT_YET_VALID` to drive the UI — the desktop's local
constant uses spaces (`"NOT YET VALID"`), so a server-sent `NOT_YET_VALID`
without `valid_from` degrades to a silent RED instead of ORANGE. Sending
`valid_from` makes behavior correct regardless.

---

## 6. Vehicle lookup and registration

**GET `/vehicles/lookup/{plate}`** — any authenticated user. Path plate is
normalized server-side. Response: `VehicleDetails` (no `updated_at`).
404 `"Plate not found"` → desktop shows ORANGE "new vehicle".
Called only when online and the guard has "check online" enabled; the local
cache always answers first.

**POST `/vehicles/register-visitor`** — guard or admin. The desktop's on-the-spot
registration (ORANGE state → Register dialog). **Online-only by design** — no
offline fallback exists for registrations.
Request: `{ "plate_number": str }` + optional `owner_first_name, owner_last_name,
phone, vehicle_make, vehicle_model, vehicle_color, note, valid_to` (absolute
epoch; desktop offers 1/3/7/30 days).
Rules: default validity 24 h; cap 30 days (422 beyond); `valid_to` in the past →
422; forces `status=ALLOWED`, `relationship=VISITOR`; **partial merge** (only
provided fields overwrite); blacklisted plate → **409** (desktop reacts by
flipping straight into the RED alarm state); audit record required (who/when).
Response: `{ "ok": true, "vehicle": VehicleDetails, "registration": {…audit…} }`.
On success the desktop upserts `vehicle` into its cache immediately, so the next
detection of that plate is GREEN without waiting for a sync.

**POST `/permits/temporary`** — guard or admin. The offline-capable, lighter
"temp permit" path (desktop also has a purely local fallback that just logs an
ALLOW event when offline).
Request: `{ "plate_number": str, "expires_in_seconds": int (1…86400) }` +
optional `owner_name, reason_id, reason_text, note`.
Rules: relative expiry (≤ 24 h); forces `status=ALLOWED`; blacklisted → **409**;
audited. Response: `{ "ok": true, "vehicle": VehicleDetails, "permit": {…} }`.

**POST `/vehicles/register`** — **admin only**, full-row replacement semantics.
Portal back-office use; the desktop never calls it.

**DELETE `/vehicles/{plate}`** — **admin only**. Removes the entry and writes a
tombstone (`deleted_at = now`) that must surface in allowlist delta `deleted[]`.
Response: `{ "ok": true, "plate_number": "<canonical>", "deleted_at": int }`.

**POST `/vehicles/bulk-upload`** — **admin only**, multipart CSV (field `file`).
Columns: `plate_number, status, owner_first_name, owner_last_name, relationship,
department, phone, vehicle_make, vehicle_model, vehicle_color, valid_from,
valid_to, note` (header required, case-insensitive; unknown columns ignored;
blank cells preserve existing values — partial merge). Dates: epoch seconds or
`YYYY-MM-DD` (UTC midnight). Limits: 5 MB / 10 000 rows. Rows are processed
independently; response reports per-row errors with 1-based row numbers:
`{ ok, total, imported, updated, failed, errors: [{row, plate_number, error}], version }`.
All accepted rows share one `updated_at` so gates pick the batch up in a single
delta.
**GET `/vehicles/bulk-upload/template`** — admin; returns the CSV header + two
example rows as a download.

---

## 7. Events (gate decisions)

**POST `/events/batch`** — any authenticated user.
Request: `{ "items": [ EventRequest, … ] }` — the desktop sends ≤ 50 per request,
oldest first. Each item:

| Field | Type | Notes |
|---|---|---|
| `id` | str (UUID4) | **required — idempotency key** |
| `event_time` | int epoch | required; decision moment (may be old for offline events) |
| `device_id` | str | required |
| `gate_id`, `lane_id` | str | required; server-assigned values from `/devices/check` |
| `direction` | `"ENTRY"` \| `"EXIT"` | required |
| `plate_number_raw` | str | required; raw OCR text or typed input, verbatim |
| `plate_number_final` | str | required; canonical form |
| `confidence` | float \| null | OCR confidence; null for manual entry |
| `decision` | `"ALLOW"` \| `"DENY"` | required (`NEED_MANUAL` accepted but never sent) |
| `decision_source` | `"AUTO"` \| `"MANUAL"` | `AUTO` only from the auto-allow countdown |
| `manual_by_user_id` | str \| null | guard's `user.uuid` |
| `manual_by_username` | str \| null | guard's email |
| `manual_reason_id` | int \| null | from `/sync/manual-reasons` |
| `manual_reason_text` | str \| null | |
| `manual_note` | str \| null | |
| `is_offline_event` | bool | true if the app was offline at decision time |
| `evidence_uploaded_url` | null | always null at ingest; set later via evidence upload |

Response: `{ "ok": true, "received_at": int, "results": [ { "ok": bool,
"event_id": str, "received_at": int, "deduped": bool, "presence_update":
{ "plate_number", "inside_status": "INSIDE"|"OUTSIDE", "updated_at" } | null,
"error": str | null } ] }`.

Server rules:
- Per-item isolation: one bad item must not fail the batch (`ok: false` + `error`).
- Idempotent on `id` (see §1); duplicate → `deduped: true`, original `received_at`.
- **Presence tracking**: `decision=ALLOW` + `direction=ENTRY` → plate is `INSIDE`;
  `EXIT` → `OUTSIDE`. Guard the update so an out-of-order (older `event_time`)
  offline event cannot regress newer state. Return `presence_update` only when
  this event actually won.

Desktop retry behavior: an event missing from `results` stays queued; `ok: false`
increments its attempt counter; after **10 failed attempts** the event is
permanently dropped — so reject events only for genuinely unrecoverable reasons.

**POST `/events`** (single) exists in the contract but the desktop only uses the
batch endpoint.

---

## 8. Evidence photos

Two-step flow, per synced event (desktop uploads ≤ 20 per cycle, ≤ 5 attempts each):

**POST `/events/{event_id}/evidence/upload-url`** — any authenticated user, no body.
Response: `{ "ok": true, "event_id": str, "upload_method": "multipart" |
"presigned_put", "upload_url": str, "file_url": str }`. 404 if the event is unknown.
- `multipart` + relative `upload_url` → desktop POSTs `multipart/form-data`
  (field **`file`**, JPEG) to `base_url + upload_url` **with** the bearer token.
- `presigned_put` + absolute URL → desktop PUTs raw bytes with
  `Content-Type: image/jpeg` and **no** Authorization header (S3/GCS-style).
  This is the intended production mode.

**POST `/events/{event_id}/evidence`** (reference-server multipart target) —
response `{ "ok": true, "event_id": str, "file_url": str, "size_bytes": int }`.
The server should persist `file_url` onto the event record.

---

## 9. Error contract

All errors: `{ "detail": "<message>" }` (string), except validation errors
(422) where `detail` is a list of field-error objects. The desktop reads
`detail`→`message`→`error`→raw text, so keep `detail` a string outside 422.

| Code | Meaning / notable cases |
|---|---|
| 400 | bad `since_version`; empty plate after normalization |
| 401 | missing/invalid/expired token; bad login; bad/used/expired one-time code; bad refresh token. Desktop: refresh + one retry, then re-login |
| 403 | role denied (`Admin access required` / `Guard or admin access required`) |
| 404 | unknown plate (lookup), unknown event (evidence), unknown device (heartbeat) |
| 409 | blacklisted plate in register-visitor / temporary permit — desktop triggers the alarm state |
| 413 | bulk CSV > 5 MB |
| 422 | validation; `valid_to` in the past / beyond cap; CSV encoding/columns/row-count |

`/devices/check` never 404s — unknown device is `200 {"registered": false}`.

---

## 10. UI action → API call map (quick reference)

| Desktop UI action | API calls |
|---|---|
| Sign in | `POST /auth/desktop/start` → `POST /auth/desktop/exchange` → `POST /devices/register` → `POST /devices/check` |
| Background (always, while logged in) | `GET /sync/allowlist` → `GET /sync/manual-reasons` → `POST /events/batch` → evidence uploads → `POST /devices/heartbeat` (every 5th cycle) |
| Plate detected / "Check status" | local cache; if online + enabled: `GET /vehicles/lookup/{plate}` |
| ALLOW / DENY button, auto-allow countdown | none directly — queued locally, next `POST /events/batch` |
| "Add Temporary Permit" | `POST /permits/temporary` (online) / local ALLOW event (offline) |
| "Register vehicle" (orange state) | `POST /vehicles/register-visitor` (online-only) |
| Token expiry (any 401) | `POST /auth/refresh`, retry once |
| Settings save / Logout | no HTTP |

Portal-side (not called by the desktop): `POST /vehicles/register`,
`DELETE /vehicles/{plate}`, `POST /vehicles/bulk-upload` (+ template),
`POST /auth/login`.

---

## 11. Implementation notes for the portal team

1. **Send `valid_from`/`valid_to` always** (see §5 warning) — the desktop derives
   expiry/not-yet-valid locally; the literal `NOT_YET_VALID` string alone is not
   enough for correct UI behavior on current clients.
2. **`expires_in` must be accurate** — proactive refresh is scheduled from it
   (80% of TTL). The reference value is 900 s.
3. **Version watermark** — `version` / `updated_at` / `deleted_at` must share one
   monotonic integer clock (epoch seconds recommended). The delta comparison is
   inclusive (`>=`), so duplicate delivery is expected and harmless; deletion
   tombstones must persist at least as long as any gate could stay offline.
4. **`mac_address` on `/devices/register`**: current clients may compute a null
   MAC on some hardware (locally-administered addresses). Accept
   `mac_address: null` rather than rejecting with 422.
5. **Evidence in production** should use `presigned_put` with real object
   storage; the desktop already supports it unchanged. Ensure the storage URL is
   reachable from gate PCs and that `file_url` is recorded on the event.
6. **Account security**: the reference server implements no login rate limiting,
   no token revocation, and serves evidence files unauthenticated from
   `/mock-storage` — all three must be addressed in production.
7. **Batch limits**: desktop sends ≤ 50 events / request; the reference server
   enforces no limit — production should cap generously (e.g. 200) and never
   below 50.
8. Seeded reference accounts: `admin@university.edu / Admin123!`,
   `guard@university.edu / Guard123!`.
