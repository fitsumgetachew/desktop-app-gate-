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

3. Run the app:

```bash
python -m smart_gate
```

## Configuration
The app loads configuration from:
1. `APP_CONFIG_PATH` if set
2. `.env` in the current working directory
3. OS-specific app data config at `.../SmartGate/config/app.env`

Key settings in `.env`:
- `API_BASE_URL`
- `AUTH_ENDPOINT`, `DEVICES_REGISTER_ENDPOINT`, `SYNC_ALLOWLIST_ENDPOINT`, `SYNC_MANUAL_REASONS_ENDPOINT`, `EVENTS_ENDPOINT`, `DEVICES_HEARTBEAT_ENDPOINT`
- `VEHICLES_LOOKUP_ENDPOINT`, `VEHICLES_REGISTER_ENDPOINT`
- `GATE_ID`, `LANE_ID`, `DIRECTION` (`ENTRY` or `EXIT`)
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
- Allowlist is cached locally and refreshed on sync.
- If API calls fail, the app marks offline and retries with backoff.
- “Sync Now” triggers immediate pull/push without blocking the UI.

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
