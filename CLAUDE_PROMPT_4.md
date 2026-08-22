# Task 4 — Staff face attendance: data, recognition and sync (no UI changes)

This is the **first of two** prompts for the dual-function gate station. It builds
the whole attendance engine — schema, roster sync, embeddings, recognition,
punch queue — and leaves the UI untouched. Prompt 5 wires it into the screen,
adds the car-without-attendance voice notice and the visual barrier signal.

**Nothing here may change gate behaviour, sync semantics, or mock mode.** This
feature adds; it does not alter.

## Phase 1 — Understand what you are building on

Read first:

- `INTEGRATION_GUIDE.md` — the live contract (§4 allowlist sync is the pattern the
  roster follows; §7 events is the pattern punches follow).
- `smart_gate/services/sync_service.py` — the cycle, per-step health reporting
  (`HEALTH_OK/HEALTH_DEGRADED`, `_describe_failure`), backoff, heartbeat.
- `smart_gate/repositories/event_repo.py` — the offline-outbox pattern to mirror
  (`MAX_SYNC_ATTEMPTS`, `list_unsynced(limit)`, `mark_synced`,
  `increment_sync_attempt`).
- `smart_gate/repositories/db.py` — additive `ALTER TABLE` migration style.
- `smart_gate/services/camera_service.py` — the QThread capture-worker pattern,
  frame throttling, graceful degradation when a model fails to load.
- `smart_gate/services/worker_context.py` — every worker thread owns its SQLite
  connection **and** its `ApiClient`.
- `smart_gate/services/api_client.py`, `smart_gate/utils/config.py`
  (`DEFAULT_ENDPOINTS` + `save_config`), `smart_gate/utils/plates.py`.
- Reference implementation to copy the matching maths from:
  `~/Software-Projects/SIT/attendance-system/web_app/app.py` →
  `verify_face_with_confidence` (strict tolerance **0.45**, min confidence
  **55.0**, group distances per person and take each person's best).

Run tests with `.venv/bin/python -m pytest tests/` (**176 passing** — keep them
green). App: `python -m smart_gate`.

### Already done for you (do not redo)

The dependencies are installed in `.venv` and verified working on this machine:
`dlib==20.0.0`, `face-recognition==1.3.0`, `face_recognition_models==0.3.0`,
`pyttsx3`, and the `setuptools<81` pin the student repo documents. **Add these to
`requirements.txt`** (dlib has no wheels for this platform — it compiles from
source and needs `cmake`/`g++`, both present here; note that in a comment).

### Measured facts you must design around

On this machine, with the real reference photos:

| Operation | Cost |
|---|---|
| Encoding a roster photo (sync time) | ~350 ms each |
| `face_locations` on 640×480 | 220 ms → **3.7 fps ceiling** |
| `face_locations` on 320×240 (half) | 67 ms → 9.3 fps, face still found |
| Encode a located face | ~40–60 ms |
| Same-person match (leave-one-out) | distance **0.245**, confidence **75.5%** → matches at 0.45/55.0 |

Two consequences, both mandatory:

1. **Detect on a half-scale frame** (`cv2.resize(frame, (0,0), fx=0.5, fy=0.5)`),
   scale the returned box coordinates back up, and use the default HOG model
   (never CNN — it needs a GPU). Throttle the face pipeline to **~3 fps** by
   timestamp, exactly as `ALPR_MAX_FPS` does. The ALPR thread already runs a
   640×640 ONNX pass + PaddleOCR at 5 fps; at full scale the two pipelines
   would fight for the CPU and both would stutter.
2. **A roster photo may yield no encoding.** In the reference set, 4 of 5 photos
   encoded — the profile shot produced none. Never assume 5 encodings per staff:
   store what you get, log a warning naming the staff when a photo yields none,
   and log an error if a staff ends up with **zero** (they cannot be recognised
   at all, and that is an operational problem someone must fix in the portal).

## Phase 2 — Implement

### 1. Schema (additive migrations in `db.py`, same style as the existing ones)

- `staff_roster` — `staff_uid` TEXT PK, `full_name` TEXT, `updated_at` INTEGER,
  `version` INTEGER (the response-level version, exactly as `cache_allowlist`
  stores it so `get_last_version()` can drive the next delta).
- `staff_plates` — `plate_number` TEXT (canonical, via `normalize_plate`),
  `staff_uid` TEXT, PK `(plate_number, staff_uid)`. Index on `plate_number`:
  prompt 5 joins on it for every ALLOW+ENTRY decision.
- `staff_photos` — `staff_uid` TEXT, `position` INTEGER (1–5), `photo_hash` TEXT,
  `encoding` BLOB (the 128-d float64 vector, `np.ndarray.tobytes()`),
  `encoded_at` INTEGER, PK `(staff_uid, position)`. The hash is what makes a
  re-download unnecessary; the blob is what makes recognition free at punch time.
- `punch_queue` — mirror `event_queue` field-for-field where it makes sense:
  `id` TEXT PK (client uuid4 — the idempotency key), `staff_uid`, `punch_time`
  INTEGER, `method` TEXT (`"face"`), `confidence` REAL, `device_id`, `gate_id`,
  `lane_id`, `synced` INTEGER DEFAULT 0, `sync_attempts` INTEGER DEFAULT 0,
  `last_sync_error` TEXT, `created_at` INTEGER. Index `(synced, sync_attempts)`.

Photos are biometric data: store the downloaded JPEGs under the app-data dir
(`utils/paths.py` pattern, e.g. `staff_photos/<staff_uid>/<position>.jpg`) so a
future re-embedding needs no network, and never log photo URLs or file contents.

### 2. Roster sync — a new step in the existing cycle

`GET /sync/staff-roster?since_version=<int>` with the gate bearer token. Add
`SYNC_STAFF_ROSTER` → `/sync/staff-roster` and `ATTENDANCE_BATCH` →
`/attendance/batch` to `DEFAULT_ENDPOINTS`, **and to `save_config()`** — it has
historically dropped keys it did not know about, which silently reverts endpoint
overrides the first time a guard saves Settings. Add a round-trip test.

Response shape. The portal is building this in parallel, so build against the
recorded fixtures that already exist — **use these, do not invent new ones**:

- `tests/fixtures/staff_roster_full.json` — 3 staff with **5, 2 and 1** photos
  respectively (proof that "5 photos" is a maximum, not a guarantee), one with
  two plates, one with none.
- `tests/fixtures/staff_roster_delta.json` — the same `stf-0001` with **every
  URL rotated but only position 2's hash changed** (so exactly one download must
  happen), its plate list reduced from two to one (the dropped plate must be
  evicted), a brand-new `stf-0004`, and `deleted: ["stf-0003"]`.

Those two files encode most of the rules below; make the tests assert against
them.

```json
{ "version": "<int-string>",
  "items": [ { "staff_uid": "…", "full_name": "…",
               "photos": [ { "position": 1, "hash": "<sha256>", "url": "<signed GET url>" } ],
               "plates": ["ABC1234"], "updated_at": 1765500000 } ],
  "deleted": ["<staff_uid>"] }
```

Rules, mirroring `_sync_allowlist` exactly:

- `since_version = MAX(version)` from `staff_roster`; `None` → **full sync**,
  which replaces the whole roster (and drops embeddings for staff no longer in
  it). Otherwise delta: upsert `items`, evict `deleted`.
- **Eviction removes the staff's photos and embeddings too** — a de-rostered
  person must not stay recognisable on a gate PC.
- Canonicalise every plate with `normalize_plate` before storing.
- **Download a photo only when its hash differs from the stored one.** URLs are
  freshly signed each sync, so never cache a URL and never treat a URL change as
  a content change — the hash is the only truth.
- **Do not send the bearer token to a signed photo URL.** Same reasoning as the
  presigned-PUT branch in `api_client.upload_evidence`: the signature is the
  credential, and an extra Authorization header is at best ignored and at worst
  rejected. Add `download_photo(url)` to `ApiClient` with a sane timeout (~30 s).
- Compute the encoding immediately after a successful download, store the blob,
  discard nothing else. **Never encode at recognition time.**

**Failure policy — deliberately different from the allowlist.** The allowlist
defers its error and re-raises so the gate drops offline; attendance must not do
that. A roster failure is a **soft-fail**: log it, set the cycle's `health_error`
(so the heartbeat reports `DEGRADED` with e.g. `"staff roster sync failed: HTTP
503"`), and let the cycle continue. A portal problem with the staff roster must
never take the barrier offline. Put that reasoning in a comment.

### 3. Face recognition service

New `smart_gate/services/face_recognition_service.py`, split so the maths is
testable without a camera:

- `encode_photo(image_bytes | path) -> np.ndarray | None` — sync-time embedding.
- `identify(probe_encoding, known) -> FaceMatch | None` — a **pure function**
  mirroring `verify_face_with_confidence`: group distances per `staff_uid`, take
  each person's best (lowest) distance, `confidence = max(0, (1 - best) * 100)`,
  and require `best <= 0.45 AND confidence >= 55.0`. Return
  `FaceMatch(staff_uid, full_name, confidence, distance)` or `None`. Keep the
  thresholds as named module constants with the reference file cited in a
  comment.
- An in-memory index loaded once from `staff_photos` and refreshed after each
  roster sync — recognition must never touch SQLite per frame.

New `smart_gate/services/face_camera_service.py` — a `QThread` worker following
`camera_service.py`: opens the **webcam** (`FACE_CAMERA_INDEX`, default 0 —
independent of the ALPR camera settings), throttles to `FACE_MAX_FPS` (~3),
detects at half scale, encodes only when a face is found, and emits
`face_recognised(FaceMatch)` / `face_unrecognised()`. Degrade exactly like the
ALPR worker: if the model or camera fails, log a warning, emit a status, and keep
the app running rather than crashing. Emit signals only — no DB writes in the
worker thread beyond the punch service call described below.

### 4. Punch recording — the event-queue pattern, verbatim

`smart_gate/repositories/punch_repo.py` + a small service:

- On a match, write a punch row with a client `uuid4` id (idempotency key), the
  match confidence, `method="face"`, and the current device/gate/lane.
- **Suppression: one punch per staff per 5 minutes**, checked locally against
  *all* local punches (synced or not) — someone standing in front of the camera
  must not punch thirty times. Make the window a named constant
  (`PUNCH_SUPPRESSION_SECONDS = 300`) and configurable later.
- A drain step in the sync cycle: `list_unsynced(limit=200)` →
  `POST /attendance/batch {"items": [...]}` → per-item results exactly like
  `/events/batch` (`ok`, `event_id`-equivalent id, `deduped`); mark synced or
  `increment_sync_attempt`, capped by `MAX_SYNC_ATTEMPTS`. Soft-fail like the
  roster: attendance must never take the gate offline.
- Expose `punches_today(staff_uid)` and `punch_count_today()` — prompt 5 needs
  both (the car notice, and the panel's counter). "Today" is the **local**
  calendar day, not UTC.

### 5. Config keys (all with sane defaults, all in `save_config`)

`FACE_ATTENDANCE_ENABLED` (default `true`), `FACE_CAMERA_INDEX` (0),
`FACE_MAX_FPS` (3), `FACE_TOLERANCE` (0.45), `FACE_MIN_CONFIDENCE` (55.0).
Follow the `AUTO_ALLOW_SECONDS` precedent: parse defensively, clamp, and never
let a bad value crash startup.

## Phase 3 — Test

Extend the suite (pure logic; no camera, no Qt event loop, no network):

- **Embedding cache**: a photo whose hash is unchanged is **not** re-downloaded or
  re-encoded; a changed hash recomputes and replaces. Assert the download stub's
  call count — that is the whole point of the cache.
- **Roster full vs delta**: full sync replaces; delta upserts and evicts; eviction
  removes that staff's photos, encodings and plates.
- **A photo that yields no encoding** is skipped without failing the sync, and a
  staff with zero usable encodings is logged.
- **Matching**: same-person encodings match at the reference thresholds; a
  distance just over 0.45 or confidence just under 55.0 is rejected; empty index
  returns `None`. Use synthetic 128-d vectors — do not ship face images in tests.
- **Punch queue**: idempotent drain (ids preserved, `deduped` respected),
  attempt counting and the `MAX_SYNC_ATTEMPTS` cap, and the **5-minute
  suppression** (second match inside the window writes nothing; after the window
  it writes).
- **Soft-fail**: a roster or attendance HTTP failure degrades the heartbeat but
  does **not** raise out of `_sync_once` and does not take the app offline —
  contrast with the allowlist test in `tests/test_sync_health.py`.
- **Mock mode unchanged**: the existing 176 tests still pass, and a clean cycle
  against the reference server still reports `status: OK`.

Finish with `python -m pytest tests/` all green and one
`QT_QPA_PLATFORM=offscreen timeout 20 python -m smart_gate` launch to prove the
app still starts (exit 124 = healthy; it exits 0 immediately if something is
wrong).
