# Task: Fix integration and product-readiness issues in the Smart Gate desktop app

## Phase 1 — Understand the repository first

Before changing anything, read and understand this repo:

- `smart_gate/main.py` — composition root, `AppWindow`, `LoginWorker`, decision handling
- `smart_gate/services/` — `api_client.py`, `sync_service.py` (offline sync engine),
  `alpr_pipeline.py` (ONNX detector + PaddleOCR), `camera_service.py`, `auth_service.py`,
  `device_service.py`
- `smart_gate/repositories/` — SQLite tables (`db.py` has the schema)
- `smart_gate/utils/config.py` — `.env` loading and `save_config`
- `smart_gate/ui/` — views; `API_REFERENCE.md` and `INTEGRATION_GUIDE.md`

Context: PySide6 gate-control app for SIT university. Camera → ALPR (plate detection +
OCR) → whitelist/blacklist decision → event logged to a local outbox → synced to a
backend. The backend today is the mock FastAPI server at
`../SIT-gate-desktopapp` (run: `uvicorn app.main:app --port 8000`; login
`guard@university.edu / Guard123!`). The server is being updated in a parallel session
with the exact contract changes referenced below — implement the client side to match.

Environment notes: use `.venv` in this repo (`source .venv/bin/activate`); deps are
already installed from the curated `requirements.txt`. Run the app with
`python -m smart_gate`. Tests: `python -m pytest tests/`.

## Phase 2 — Implement these changes (in this order)

### 1. Canonical plate normalization (critical)
The ALPR pipeline emits `ABC1234` but the server/cache store `ABC-1234`, so AI-detected
plates miss every lookup. Fix:

- Create one shared helper `normalize_plate(s) -> str` (uppercase, strip all
  non-alphanumerics) in `smart_gate/utils/` and use it EVERYWHERE a plate is compared,
  stored, or sent: `alpr_pipeline._normalize`, `main._normalize_plate`, allowlist cache
  lookups, `/vehicles/lookup` calls, event payloads. The server is being changed to store
  the same canonical form.
- Raise `min_plate_length` in `alpr_pipeline.py` from 2 to 5 so garbage fragments are not
  committed.

### 2. Blacklist handling (critical)
`BLACKLISTED` status exists in the data but no code treats it specially. Implement:

- On a plate whose cached/server status is `BLACKLISTED`: pre-select DENY, show a
  prominent, visually distinct alarm state in the main view (different from a normal
  `DENIED`), and require a manual reason to override with ALLOW.
- The server now also returns `"alert": true` on such items — use it if present, but do
  not depend on it (fall back to the status string).

### 3. Allowlist revocation (critical)
The delta sync only upserts — revoked plates stay `ALLOWED` locally forever. The server
now returns `"deleted": ["PLATE1", ...]` in `GET /sync/allowlist`. In
`sync_service._sync_once`: delete those plates from `cache_allowlist`. When doing a full
sync (no `since_version`), replace the whole cache instead of merging.

### 4. Temporary permits (critical)
The app calls admin-only `POST /vehicles/register` from a guard session → 403. The server
now provides `POST /permits/temporary`
(`{plate_number, owner_name?, reason_id?, reason_text?, note?, expires_in_seconds}`,
guard-accessible, ≤86400 s, 409 if the plate is blacklisted). Switch the temp-permit UI
flow to it, handle the 409 with a clear message, and drop the guard-side call to
`/vehicles/register`.

### 5. Expiry enforcement offline
`_handle_check_status` and the decision path never compare `valid_to` to now — an expired
cached permit still reads as ALLOWED offline. Treat `valid_to < now` as `EXPIRED`
everywhere a status is displayed or used for a decision.

### 6. Fail-closed device check
In `LoginWorker` (`main.py` ~line 76), a failed `/devices/check` defaults
`device_registered = True`. Distinguish: network error → proceed in explicit
"offline mode" (banner in UI); server responds `registered: false` → block login with a
clear message. Also apply the gate/lane the server returns from `/devices/check` instead
of ignoring it (server assignment is authoritative; update local config/device row and
log if it differs).

### 7. Config correctness
- `save_config` (`utils/config.py`) writes back only 8 of the 13 endpoint keys, silently
  dropping `AUTH_DESKTOP_*`, `AUTH_REFRESH`, `DEVICES_CHECK`, `EVENTS_BATCH` overrides on
  first Settings save. Persist all keys.
- Refresh `.env.example` (and `.env` keys documented in README) to the current endpoint
  set — it still lists the old `/auth/login`-era keys.
- Wrap `int()` parses of `CAMERA_INDEX` / `SYNC_INTERVAL_SECONDS` so a bad value falls
  back to the default with a warning instead of crashing startup.

### 8. Token handling
- Refresh the access token proactively (its TTL is 900 s; refresh at ~80% of TTL in the
  sync loop) instead of only reacting to 401s. `LookupWorker` and the temp-permit worker
  currently do not handle 401 at all — route them through the same refresh-and-retry
  helper.
- Stop persisting the access token to SQLite (keep it in memory); keep the refresh token
  persisted for now but add a `TODO(security)` noting it must move to OS keyring before
  production.

### 9. ALPR / camera performance
`camera_service.py` runs the full detector + OCR synchronously on every frame. Process at
most ~5 frames/second (skip frames by timestamp), and skip OCR entirely when no detection
box is found. Keep the preview stream at full rate.

### 10. Thread-safety cleanups
- `requests.Session` is shared across UI thread + 3 worker threads — give the sync worker
  its own `ApiClient` (or a session per thread).
- Disable the login button while a `LoginWorker` is running to prevent concurrent
  workers.

## Phase 3 — Test

- Extend `tests/` with unit tests for: `normalize_plate`, expiry logic, revocation
  handling in the sync merge, and the blacklist decision path (pure logic — no Qt event
  loop needed; factor logic out of widgets where necessary).
- Start the mock server (`cd ../SIT-gate-desktopapp && uvicorn app.main:app --port 8000`)
  and run a headless end-to-end check with the app's own service classes: login →
  register → check → allowlist sync (including a deletion) → temp permit → events batch →
  evidence upload → refresh. All steps must pass.
- Run `python -m pytest tests/` and make sure everything passes.
- Launch `python -m smart_gate` once to confirm the app still starts cleanly.
