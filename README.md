# Smart Gate Desktop (Vehicle Lane MVP)

Production-ready Python desktop application for University Smart Gate Vehicle Access. Built with PySide6 + OpenCV, offline-first, and bundle-ready for Linux/Windows/macOS.

## Features
- Guard login (email + password)
- Device identity (UUID-based) stored locally
- Offline-first event queue + allowlist cache (SQLite)
- Background sync: pull allowlist, push queued events
- Camera integration: USB webcam or RTSP/IP camera
- Manual plate entry with ALLOW/DENY decisions
- Plate status check (local cache + optional online lookup)
- Evidence capture to local disk
- Logout + in-app settings page

## Project Structure
- `smart_gate/ui` UI widgets and screens
- `smart_gate/services` API, sync, camera, device, auth services
- `smart_gate/repositories` SQLite repositories
- `smart_gate/models` dataclasses
- `smart_gate/utils` config, paths, logging

## Setup (Ubuntu)
1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create a config file:

```bash
cp .env.example .env
```

3. Choose how operators sign in (see **Sign-in modes** below), then run the app:

```bash
python -m smart_gate
```

## Sign-in modes

The login screen you get is decided by one setting, `AUTH_MODE`. If the app is
showing an **email and password** form when you expected the SIT portal, this is
why — `AUTH_MODE` is still `mock`.

| `AUTH_MODE` | Login screen | Use it for |
|---|---|---|
| `mock` (default) | Email + password | The local reference server during development |
| `portal` | "Sign in via SIT Portal" + a code box | The real SIT portal, and every deployed gate |

### Signing in with the SIT portal

In `.env`:

```bash
AUTH_MODE=portal
API_BASE_URL=https://sit-portal-e6750.web.app/api/gate
PORTAL_SSO_URL=https://sit-portal-e6750.web.app/sso
```

Then `python -m smart_gate` and sign in like this:

1. Press **Sign in via SIT Portal**. The system browser opens the portal page
   for *this* machine (the app appends its own `device_id`). No browser on the
   gate PC? The full link is printed on the login screen — open it on a phone.
2. Sign in on the portal. It shows a **one-time code**, displayed in groups of
   four. The code is valid for **120 seconds**.
3. Type or paste the code into the app and press **Continue**. Spaces don't
   matter — they're stripped.

The desktop never sees a password in this mode: the portal authenticates the
operator and only the one-time code reaches the app.

**The device must be provisioned first.** The portal identifies this machine by
its `device_id`, shown with a **Copy** button on the login screen and again in
Settings. Copy it — don't retype it. A single transposed character produces a
device that signs in perfectly and then never matches its provisioning record,
which surfaces much later as permanent offline mode with nothing on screen
naming the cause.

Common messages and what they mean:

| Message | Cause |
|---|---|
| "Invalid or expired code" | Older than 120 s, already used, or minted for a different `device_id`. Generate a new one. |
| "These credentials were issued for a different device" | The session belongs to another machine. Sign in again from this one. |
| "This device has been de-provisioned by an administrator" | The portal deleted this device's record. Contact IT to re-provision. |
| "device check not available — continuing in offline mode" | Normal against a server without `/devices/check`. The gate keeps working. |

### Going back to the mock server

```bash
AUTH_MODE=mock
API_BASE_URL=http://localhost:8000
```

Seeded accounts are `guard@university.edu / Guard123!` and
`admin@university.edu / Admin123!` (see **Mock API** below).

`AUTH_MODE` can also be changed in **Settings → Authentication** without editing
the file; it takes effect on the next sign-in.

## Configuration
The app loads configuration from:
1. `APP_CONFIG_PATH` if set
2. `.env` in the current working directory
3. OS-specific app data config at `.../SmartGate/config/app.env`

Key settings in `.env` (see `.env.example` for the full, current list):
- `API_BASE_URL`
- `AUTH_MODE` (`mock` or `portal`) and `PORTAL_SSO_URL` — see **Sign-in modes** above
- Auth: `AUTH_ENDPOINT` (legacy direct login), `AUTH_DESKTOP_START_ENDPOINT`, `AUTH_DESKTOP_EXCHANGE_ENDPOINT`, `AUTH_REFRESH_ENDPOINT`, `AUTH_LOGOUT_ENDPOINT`
- Devices: `DEVICES_REGISTER_ENDPOINT`, `DEVICES_CHECK_ENDPOINT`, `DEVICES_HEARTBEAT_ENDPOINT`
- Sync: `SYNC_ALLOWLIST_ENDPOINT`, `SYNC_MANUAL_REASONS_ENDPOINT`
- Events: `EVENTS_ENDPOINT`, `EVENTS_BATCH_ENDPOINT`
- Vehicles/permits: `VEHICLES_LOOKUP_ENDPOINT`, `PERMITS_TEMPORARY_ENDPOINT`
- `GATE_ID`, `LANE_ID`, `DIRECTION` (`ENTRY` or `EXIT`) — the gate/lane the server returns from `/devices/check` overrides the local values at sign-in
- `CAMERA_MODE` (`USB` or `RTSP`)
- `CAMERA_INDEX` (USB camera index)
- `CAMERA_RTSP_URL` (RTSP URL)
- `EVIDENCE_DIR` (optional, defaults to app data dir)
- `SYNC_INTERVAL_SECONDS`
- `ENV_MODE` (`DEV` or `PROD`)

Switching from mock to real API is a config-only change (base URL/endpoints). No code changes required.

## Offline Mode
- Every event is stored in `event_queue` first with `synced=0`.
- Sync worker pushes unsynced events when online.
- Allowlist is cached locally and refreshed on sync. A delta sync applies both
  the `items` upserts and the `deleted` revocations; a full sync (no
  `since_version`) replaces the cache outright.
- Cached permits whose `valid_to` has passed read as `EXPIRED`, offline included.
- If API calls fail, the app marks offline and retries with backoff.
- “Sync Now” triggers immediate pull/push without blocking the UI.
- If `/devices/check` is unreachable at sign-in the app enters an explicitly
  flagged offline mode (yellow banner). If the server answers
  `registered: false`, login is blocked.

## Plate Handling
All plates are normalized to one canonical form — uppercase, alphanumeric only
(`abc-1234` → `ABC1234`) — by `smart_gate/utils/plates.py`. The ALPR pipeline,
the cache, lookups and event payloads all use it, and the server stores the
same form.

## Traffic-Light Decision States
When the ALPR commits a plate it is classified against the local cache (with an
online `/vehicles/lookup` fallback) and the camera section is driven into one of
three states — a thick coloured border around the video plus a banner carrying
the plate, owner and relationship. The classifier lives in
`services/decision_state.py` and is pure logic (no Qt), so it is unit-tested
directly.

| State | When | Behaviour |
|---|---|---|
| **GREEN** | recognized, `ALLOWED`, inside its validity window | Counts down `AUTO_ALLOW_SECONDS` (default 5) then auto-confirms ALLOW with `decision_source="AUTO"`. A **STOP** button aborts the countdown and returns to the manual flow. Set `AUTO_ALLOW_SECONDS=0` to disable auto-continue. |
| **RED** | `BLACKLISTED` or `DENIED` | Never auto-anything. `BLACKLISTED` loops `assets/sounds/alarm.wav` until **Acknowledge alarm** is pressed; `DENIED` is red but silent. The reason-and-note override flow is unchanged. |
| **ORANGE** | plate unknown (not cached / 404), permit expired, or permit not yet valid | Offers **Register vehicle**. The reason is shown ("Permit expired …"). |

The state resets — border cleared, countdown cancelled, sound stopped — when a
decision is submitted, the guard cancels, or the ALPR commits a *different*
plate. Re-detecting the *same* plate never restarts a countdown the guard
stopped, nor re-sounds an alarm they acknowledged.

Auto-confirm is the only path that produces `decision_source="AUTO"`; a guard
pressing ALLOW/DENY always records `MANUAL`, even when the ALPR read the plate.
The auto-confirm branch in `AppWindow._on_countdown_tick` is where the
barrier-open serial command will go.

### Alarm sound
Played with `QtMultimedia.QSoundEffect` on an infinite loop. A machine with no
audio device (headless kiosk, no PulseAudio session) logs a warning and still
shows the red state — audio is an enhancement, never a prerequisite.

## On-the-Spot Registration
The ORANGE state and the toolbar's **Register Vehicle** button open a modal
dialog (plate, owner name/phone, vehicle make/model/colour, note, validity of
1/3/7/30 days) that calls `POST /vehicles/register-visitor` on a worker thread.

**Online-only by design:** if the app is offline, or the call fails with a
network error, nothing is written locally — the guard is told registration needs
a connection and pointed at the temporary-permit flow, which is explicitly the
offline path. On success the returned vehicle is cached immediately so the next
detection of that plate goes GREEN without waiting for a sync. A 409 (the plate
is blacklisted) switches straight into the RED alarm state.

## Security Notes
- The access token is kept in memory only (`services/token_store.py`) and is
  refreshed proactively at ~80% of its 900 s TTL.
- The refresh token is still persisted in SQLite — see the `TODO(security)`
  in `repositories/device_repo.py`; it must move to the OS keyring before
  production.

## Bundling (PyInstaller)
Install PyInstaller:

```bash
pip install pyinstaller
```

Build on each target OS:

```bash
pyinstaller --noconfirm --clean smart_gate.spec
```

Notes:
- Build on the target OS for best results (Windows/macOS/Linux).
- OpenCV and PySide6 require platform-specific binaries. Ensure they are installed in the build environment.
- For RTSP on Windows/macOS, ensure your OpenCV build has FFmpeg support.

## Camera Selection Notes
- USB cameras use `CAMERA_INDEX`. You may need to try `0`, `1`, etc.
- RTSP cameras use `CAMERA_RTSP_URL`. Example: `rtsp://user:pass@ip:554/stream`

## Tests
Run tests:

```bash
pytest -q
```

## App Data Locations
- Linux: `~/.local/share/SmartGate`
- macOS: `~/Library/Application Support/SmartGate`
- Windows: `%APPDATA%/University/SmartGate`

## Mock API
See `API_REFERENCE.md` for mock server contract details and seed credentials.

## UI Notes
- Top bar shows Online/Offline, user, gate/lane, Sync Now, Settings, Logout.
- Plate “Check Status” uses local cache first; optional online lookup.
- Not found actions: “Sync then re-check” or “Add Temporary Permit”.
- Local presence hint shows last known INSIDE/OUTSIDE per plate (local-only).
