# Task 5 — Attendance station UI, car-without-attendance notice, visual barrier

**Run `CLAUDE_PROMPT_4.md` first.** This prompt assumes the attendance engine
exists and is tested: staff roster sync, cached embeddings, the face recognition
worker, and the punch queue with its 5-minute suppression window.

This turns the gate app into SIT's dual-function station: **face attendance is
the main panel, plate reading moves to a sidebar**, a staff car entering without
a punch today gets a spoken reminder, and an ALLOW decision shows a visual
"barrier open" signal. Real barrier hardware is **not** in scope.

## Phase 1 — Understand what you are building on

Read first:

- `smart_gate/ui/main_view.py` — the current gate screen: camera preview,
  decision buttons, traffic-light banner (`decision_state`), recent events,
  status bar. Note `set_user`, `set_gate_lane`, `set_offline_mode`.
- `smart_gate/main.py` — `AppWindow` is the composition root: `_on_plate_detected`,
  `_submit_decision` (**this is where an ALLOW is finalised** — both the manual
  path and the auto-allow countdown land here), `_handle_decision`,
  `_connect_signals`.
- `smart_gate/services/decision_state.py` — GREEN/RED/ORANGE classification and
  `AutoAllowCountdown`.
- `smart_gate/services/alarm_service.py` — the injectable-service precedent to
  follow for text-to-speech (graceful degradation when the audio backend is
  missing, PySide6 enum gotcha documented in the file).
- The attendance engine from prompt 4: the face worker's
  `face_recognised` / `face_unrecognised` signals, `punches_today(staff_uid)`,
  `punch_count_today()`, and the `staff_plates` table.

Tests: `.venv/bin/python -m pytest tests/`. App: `python -m smart_gate`.
**Every existing gate behaviour must stay byte-identical** — decisions, sync
semantics, mock mode. This feature adds a panel; it does not change the gate.

## Phase 2 — Implement

### 1. Layout: attendance main, plates sidebar

Restructure `main_view` into two columns, keeping every existing widget alive
and every existing signal connected (rename nothing the app already wires):

- **Main panel — Staff Attendance**: the webcam feed, the recognition state, and
  today's punch count (`punch_count_today()`, refreshed on each punch and on the
  existing 5 s UI timer).
  - On a match: a large, brief confirmation — **"✓ &lt;full name&gt; — attendance
    recorded &lt;HH:MM&gt;"** on a green field, auto-clearing after a few seconds
    back to the idle "Look at the camera" state.
  - On no match: **"Not recognised"**, neutral grey. **No siren, no alarm, no red
    alert** — this is attendance, not security. Reuse nothing from the blacklist
    alarm path.
  - When a match is suppressed by the 5-minute window, say so gently
    (e.g. "&lt;name&gt; — already recorded at &lt;HH:MM&gt;") rather than showing a
    failure; the person did nothing wrong.
- **Sidebar — Gate / Plates**: the existing plate flow compacted — camera
  preview, traffic-light banner, plate field, ALLOW/DENY, recent events. It must
  keep working exactly as it does today at a smaller size; if a widget cannot
  shrink gracefully, put the sidebar in a scroll area rather than dropping
  anything.
- Both camera feeds run at once. Prompt 4 throttled the face pipeline to ~3 fps
  at half scale precisely so this is affordable; if the preview stutters, lower
  the *preview repaint* rate, never the ALPR rate.
- Respect `FACE_ATTENDANCE_ENABLED`: when off, the app must fall back to today's
  single-column gate layout with no dead space and no webcam thread.

### 2. Car-without-attendance voice notice

The join that makes this station worth building. Trigger it in `_submit_decision`,
**after** the event row is written and only when the decision is `ALLOW` and the
direction is `ENTRY`:

1. Look the final plate up in `staff_plates` (canonical form — the table is
   indexed for this).
2. If it belongs to a staff member, check `punches_today(staff_uid)`.
3. If there is **no punch today**: show a banner on the attendance panel
   (e.g. "&lt;first name&gt; has not recorded attendance today") **and** speak a short
   notice once: **"&lt;first name&gt;, please record your attendance."**
4. If they have punched today: **silent**. No banner, no speech.

Rules that are not optional:

- **Never block the gate flow.** The lookup is a single indexed local query and
  the speech happens on a worker thread or queue — the barrier signal, the event
  write and the UI must not wait on either. If TTS raises or hangs, the gate is
  unaffected.
- **Once per staff per suppression window** (reuse `PUNCH_SUPPRESSION_SECONDS`,
  or its own constant with the same default): a staff car re-detected two minutes
  later must not be nagged again.
- Fully offline: `pyttsx3` speaks through whatever the OS has, including a
  Bluetooth speaker paired at OS level. That pairing is the OS's business — the
  app just calls the API. If no audio device exists, log a warning and show the
  banner only.

Build it behind a tiny injectable interface so it is testable:

```python
class Speaker(Protocol):
    def say(self, text: str) -> None: ...
```

with a `Pyttsx3Speaker` used in production and a fake in tests. Follow
`alarm_service.py` for the degradation pattern. Never put a plate number or a
full name into a log line at info level — first names only in the spoken text.

### 3. Barrier signal — visual only

```python
class BarrierController(Protocol):
    """Signal the barrier to open.

    The signal is a convenience, never an authority: the guard keeps manual
    control at all times, and no failure of this interface may block, delay or
    hold the barrier. Callers must not wait on it and must not surface its
    errors as gate errors.
    """
    def signal_open(self) -> None: ...
```

Exactly one method is used by the app. Implement `VisualBarrierController` now:
on an ALLOW decision, flash a clear **"BARRIER OPEN SIGNAL"** indicator on the
plate sidebar for a second or two. Call it from `_submit_decision` on ALLOW,
after the event row is written, wrapped so an exception can never propagate into
the decision path.

**No hardware code, no serial, no Bluetooth, no `pyserial` dependency.** The
transports are being proven separately in
`~/Software-Projects/SIT/barrier-comm-test/` against an agreed ack-or-fail
protocol (`OPEN` → `ACK OPEN`, every command acknowledged; see its README). A
later phase lifts that proven transport behind this same interface — which is
why `signal_open()` must stay the only method the app calls.

## Phase 3 — Test

Extend the suite. Keep it pure — no camera, no audio device, no Qt event loop
where avoidable; factor logic out of widgets where necessary (the
`decision_state` module is the precedent).

- **The car-notice join**: staff plate + no punch today → notice fires **exactly
  once**, with the fake speaker receiving the expected text; punch already
  recorded today → **silent**; non-staff plate → silent; a second detection
  inside the suppression window → silent; `DENY` or `EXIT` → silent.
- **"Today" is the local calendar day** — a punch from yesterday evening does not
  count as today's.
- **Speaker failure is contained**: a `Speaker` that raises must not prevent the
  banner, the event write, or the barrier signal.
- **Barrier**: `signal_open()` is called on ALLOW and not on DENY; a controller
  that raises does not break the decision path (assert the event row is still
  written).
- **Recognition UI states** (headless where possible): match → confirmation text
  with name and time; suppressed match → the gentle "already recorded" text;
  no match → neutral "Not recognised" and **no alarm service call**.
- **Mock mode and the gate are unchanged**: the full existing suite passes, and
  the decision/sync paths behave exactly as before with
  `FACE_ATTENDANCE_ENABLED=false`.

Finish with the whole suite green, then:

```bash
QT_QPA_PLATFORM=offscreen timeout 20 python -m smart_gate   # exit 124 = healthy
python -m smart_gate                                        # eyeball both panels
```

and confirm on screen: both camera feeds live at once, a face producing a punch
and a confirmation, the plate sidebar still deciding as before, and the
"BARRIER OPEN SIGNAL" indicator firing on an ALLOW.
