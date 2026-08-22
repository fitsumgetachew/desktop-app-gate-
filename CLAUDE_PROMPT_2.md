# Task 2: Color-coded auto-decision UI + on-the-spot vehicle registration

## Phase 1 — Understand the current state first

Read before changing anything: `smart_gate/main.py` (`AppWindow`, `_on_plate_detected`,
`_handle_decision`), `smart_gate/ui/main_view.py` and `ui/theme.py` (SIT style guide
colors live here — reuse them), `smart_gate/services/permit_rules.py`,
`services/sync_service.py`, `repositories/allowlist_repo.py` + `repositories/db.py`,
`utils/plates.py`, and `tests/` (65 passing tests — keep them green).

Context: PySide6 gate app; camera → ALPR → decision → event outbox → sync. The mock
server at `../SIT-gate-desktopapp` is being extended in a parallel session with this
exact contract — richer vehicle fields (`owner_first_name`, `owner_last_name`,
`relationship`, `department`, `phone`, `vehicle_make/model/color`, `valid_from`,
`note`) on `/sync/allowlist` and `/vehicles/lookup`, plus a guard-accessible
`POST /vehicles/register-visitor` (`plate_number` required; owner/vehicle fields
optional; `valid_to` capped at 30 days; **409 if the plate is blacklisted**).
Environment: `source .venv/bin/activate`, run `python -m smart_gate`, tests
`python -m pytest tests/`. Mock server:
`cd ../SIT-gate-desktopapp && uvicorn app.main:app --port 8000`
(guard@university.edu / Guard123!).

## Phase 2 — Implement

### 1. Traffic-light decision states on the camera view (the main feature)
When the ALPR pipeline commits a plate, classify it from the local allowlist cache
(fall back to `/vehicles/lookup` when online) and drive the camera section into one of
three highly visible states — a thick colored border around the video feed PLUS a
colored banner with the plate, owner name, and relationship. Add the state colors to
`theme.py` (align with the SIT style guide palette already used there):

- **GREEN — recognized & ALLOWED (and not expired/not-yet-valid):** show
  "✓ <plate> — <owner> (<relationship>) — opening in N s". Start a visible countdown
  (default 5 s, config key `AUTO_ALLOW_SECONDS`, `0` disables auto-continue). If the
  guard does nothing, auto-confirm ALLOW when it hits zero (`decision_source="AUTO"` —
  this is the first genuinely automatic decision, and this code path is where the
  barrier-open command will later go). A prominent **STOP/CANCEL** button aborts the
  countdown and drops to the normal manual ALLOW/DENY flow (`decision_source="MANUAL"`).
- **RED — BLACKLISTED (or DENIED):** never auto-anything. Red border + banner. For
  `BLACKLISTED` additionally loop the alarm sound (see §2) until the guard
  acknowledges (button "Acknowledge alarm" stops the sound; the existing
  manual-reason-required override flow stays as is). `DENIED` is red but silent.
- **ORANGE — unknown plate (not in cache / 404 from lookup):** orange border + banner
  "New vehicle — not registered", with a **Register vehicle** button that opens the
  registration dialog (§3).

Expired (`valid_to` past) and not-yet-valid (`valid_from` future) entries are NOT
green — treat as orange with the reason shown ("Permit expired ...").

The state must reset (border cleared, countdown cancelled, sound stopped) when: a
decision is submitted, the guard cancels, or a *different* plate is committed by the
ALPR. Guard the countdown against plate changes — a new detection mid-countdown
cancels and re-evaluates. All timers/sounds live on the UI thread (QTimer), no
sleeping in slots.

### 2. Alarm sound
`smart_gate/assets/sounds/alarm.wav` already exists in the repo (2 s two-tone siren,
seamless loop). Play it with `PySide6.QtMultimedia.QSoundEffect`
(`setLoopCount(QSoundEffect.Infinite)`), stop on acknowledge/reset. QtMultimedia ships
with the installed PySide6-Addons — verify at runtime and degrade gracefully (log a
warning, still show red) if the audio backend fails, e.g. on machines without a sound
device. Add the sounds dir to `smart_gate.spec` datas so packaged builds include it.

### 3. Registration dialog (online-only)
A modal dialog launched from the ORANGE state (and from a "Register vehicle" action in
the toolbar/menu for manually typed plates):

- Fields: plate (pre-filled from ALPR, editable), owner first/last name, phone,
  vehicle make/model/color, note, validity (dropdown: 1 day / 3 days / 7 days / 30
  days). Calls `POST /vehicles/register-visitor` with the bearer token via a worker
  QThread (never block the UI; route 401 through the existing refresh-and-retry
  helper).
- **Online-only, by design:** if the last sync says offline (or the call fails with a
  network error), show "Registration requires a server connection" and do NOT write
  anything locally — no offline fallback for registrations. The existing temporary
  permit flow remains the offline path.
- On success: upsert the returned vehicle into the local cache immediately so the very
  next detection of that plate goes GREEN, and trigger a sync.
- On 409 (blacklisted): switch straight to the RED alarm state — someone just tried to
  register a blacklisted plate; the guard must know.

### 4. Show the richer details everywhere
- Extend `cache_allowlist` (additive migration in `repositories/db.py`) with the new
  fields; store them from `/sync/allowlist` items when present (tolerate their absence
  so the app still works against an older server).
- The decision banner and the vehicle-details panel show: owner full name,
  relationship, department, vehicle make/model/color, phone, note, validity window.
  Missing fields collapse instead of showing "None".

## Phase 3 — Test

- Unit tests (pure logic, no Qt event loop): the state classifier
  (ALLOWED→green, BLACKLISTED→red+alarm, DENIED→red silent, unknown→orange,
  expired/not-yet-valid→orange), countdown cancel-on-new-plate logic (factor it into a
  testable class), and the cache upsert with new fields.
- With the mock server running: headless e2e — sync richer fields into the cache,
  register a visitor via the app's client code, confirm 409 on a blacklisted plate,
  confirm the registered plate classifies GREEN afterwards.
- `python -m pytest tests/` all green (65 existing + new), and launch
  `python -m smart_gate` once to confirm the app starts and the camera view renders.
