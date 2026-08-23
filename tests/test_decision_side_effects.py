"""The gate decision path, with the attendance side effects bolted on.

These drive the **real** ``AppWindow._submit_decision`` — real widgets, a real
SQLite file, real repositories — because the property under test is precisely
that the new work happens *around* the decision without becoming part of it.
Testing a re-implementation of the sequence would prove nothing about that.

Nothing here touches the network or a camera: no worker thread is ever started,
and the sync trigger is skipped because the window is offline.
"""

import sqlite3

import pytest

from smart_gate.services import attendance_display
from PySide6 import QtWidgets

import smart_gate.main as main_module
from smart_gate.repositories.db import Database
from smart_gate.repositories.punch_repo import PunchRepository, local_day_start
from smart_gate.services.attendance_service import AttendanceService
from smart_gate.services.face_recognition_service import FaceMatch
from smart_gate.utils.config import load_config

STAFF_PLATE = "AA12345"
STAFF_UID = "stf-0001"
STAFF_NAME = "Abebe Bekele"


@pytest.fixture(scope="session")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class RecordingSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)

    def stop(self):
        pass


class RecordingBarrier:
    def __init__(self):
        self.signals = 0

    def signal_open(self):
        self.signals += 1


def _make_window(qapp, tmp_path, monkeypatch, attendance_enabled=True):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))

    class TmpDatabase(Database):
        def __init__(self, db_path=None):
            super().__init__(db_path=tmp_path / "gate.db")

    monkeypatch.setattr(main_module, "Database", TmpDatabase)

    config = load_config()
    config.face_attendance_enabled = attendance_enabled
    config.direction = "ENTRY"
    window = main_module.AppWindow(config)
    window.speaker = RecordingSpeaker()
    window.barrier = RecordingBarrier()
    return window


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path, monkeypatch)
    yield w
    w.close()


def _enrol_staff(window, plates=(STAFF_PLATE,)):
    """Put one staff member on the roster of the window's own database."""
    window.staff_repo.upsert_staff(STAFF_UID, STAFF_NAME, 1, 1)
    window.staff_repo.replace_plates(STAFF_UID, plates)


def _punch(window, when=None):
    AttendanceService(
        window.punch_repo, "dev-1", "GATE-1", "LANE-A"
    ).record_punch(
        FaceMatch(STAFF_UID, STAFF_NAME, 75.5, 0.245),
        punch_time=when if when is not None else local_day_start() + 60,
    )


def _decide(window, decision, plate=STAFF_PLATE):
    window.main_view.set_plate_text(plate)
    window._submit_decision(decision, source="MANUAL")


def _events(window):
    return window.event_repo.list_recent()


def _banner_shown(window) -> bool:
    """Whether the notice banner has been shown.

    ``isVisible()`` is False for every widget whose ancestors are unshown, and
    these windows are deliberately never shown — ``isHidden()`` reports the
    explicit show()/hide() the code actually performs.
    """
    return not window.main_view.attendance_notice_banner.isHidden()


# ── The decision itself is unchanged ──────────────────────────────────


def test_an_allow_still_writes_its_event_row(window):
    _decide(window, "ALLOW")

    rows = _events(window)
    assert len(rows) == 1
    assert rows[0]["plate_number_final"] == STAFF_PLATE
    assert rows[0]["decision"] == "ALLOW"


def test_a_deny_still_writes_its_event_row(window):
    _decide(window, "DENY")

    assert _events(window)[0]["decision"] == "DENY"


# ── Barrier ───────────────────────────────────────────────────────────


def test_the_barrier_is_signalled_on_allow(window):
    _decide(window, "ALLOW")

    assert window.barrier.signals == 1


def test_the_barrier_is_not_signalled_on_deny(window):
    _decide(window, "DENY")

    assert window.barrier.signals == 0


def test_a_barrier_that_raises_still_leaves_the_event_written(window):
    """The signal is a convenience, never an authority. By the time it is called
    the gate has already decided and the row is already committed."""

    class Boom:
        def signal_open(self):
            raise RuntimeError("serial port closed")

    window.barrier = Boom()

    _decide(window, "ALLOW")           # must not raise

    assert len(_events(window)) == 1
    assert _events(window)[0]["decision"] == "ALLOW"


# ── The car-without-attendance notice ─────────────────────────────────


def test_a_staff_car_without_a_punch_is_announced_once(window):
    _enrol_staff(window)

    _decide(window, "ALLOW")

    assert window.speaker.said == ["Abebe, please record your attendance."]
    assert _banner_shown(window)
    assert (
        window.main_view.attendance_notice_banner.text()
        == "Abebe has not recorded attendance today"
    )


def test_a_second_entry_inside_the_window_is_silent(window):
    _enrol_staff(window)
    _decide(window, "ALLOW")

    _decide(window, "ALLOW")

    assert len(window.speaker.said) == 1


def test_a_staff_member_who_already_punched_today_is_not_nagged(window):
    _enrol_staff(window)
    _punch(window)

    _decide(window, "ALLOW")

    assert window.speaker.said == []
    assert not _banner_shown(window)


def test_a_punch_from_yesterday_evening_does_not_count(window):
    """Local calendar day: under a UTC boundary an 11 p.m. punch would still
    read as 'today' at 2 a.m. and the reminder would wrongly stay silent."""
    _enrol_staff(window)
    _punch(window, when=local_day_start() - 3600)

    _decide(window, "ALLOW")

    assert window.speaker.said == ["Abebe, please record your attendance."]


def test_a_visitor_plate_is_silent(window):
    _enrol_staff(window)

    _decide(window, "ALLOW", plate="ZZ00000")

    assert window.speaker.said == []


def test_a_denied_staff_car_is_silent(window):
    _enrol_staff(window)

    _decide(window, "DENY")

    assert window.speaker.said == []


def test_an_exit_is_silent(window):
    """Leaving without a punch is not something a reminder can still fix."""
    _enrol_staff(window)
    window.config.direction = "EXIT"

    _decide(window, "ALLOW")

    assert window.speaker.said == []


def test_a_speaker_that_raises_breaks_nothing(window):
    """The banner, the event row and the barrier all survive a dead audio
    device."""
    _enrol_staff(window)

    class Boom:
        def say(self, text):
            raise RuntimeError("no audio device")

        def stop(self):
            pass

    window.speaker = Boom()

    _decide(window, "ALLOW")           # must not raise

    assert len(_events(window)) == 1
    assert window.barrier.signals == 1


def test_recording_a_punch_clears_the_reminder(window):
    """They were reminded, they walked in and punched — the banner comes down."""
    _enrol_staff(window)
    _decide(window, "ALLOW")
    assert _banner_shown(window)

    window._on_punch_recorded(STAFF_UID, STAFF_NAME, local_day_start() + 120)

    assert not _banner_shown(window)


# ── Recognition panel states ──────────────────────────────────────────


def test_a_recorded_punch_shows_a_confirmation(window):
    """The panel is three pieces now — name, outcome badge, timestamp — so the
    confirmation is asserted where each part actually lives."""
    window._on_punch_recorded(STAFF_UID, STAFF_NAME, local_day_start() + 120)

    view = window.main_view
    assert STAFF_NAME in view.attendance_state_label.text()
    assert view.attendance_badge_label.text() == attendance_display.BADGE_RECORDED
    assert "recorded" in view.attendance_detail_label.text().lower()


def test_a_suppressed_punch_shows_the_gentle_wording(window):
    """Still reassurance rather than rejection: they already punched, which is
    not a failure and must not read like one."""
    window._on_punch_suppressed(STAFF_UID, STAFF_NAME, local_day_start() + 60)

    view = window.main_view
    assert STAFF_NAME in view.attendance_state_label.text()
    assert view.attendance_badge_label.text() == attendance_display.BADGE_ALREADY
    assert "already recorded" in view.attendance_detail_label.text().lower()


def test_an_unrecognised_face_never_touches_the_alarm(window):
    """Attendance is not security. The blacklist siren must be unreachable from
    this path."""
    calls = []
    window.alarm_service.start = lambda: calls.append("start")

    window._on_face_unrecognised()

    assert window.main_view.attendance_state_label.text() == "Not recognised"
    assert calls == []


def test_the_daily_counter_follows_the_punch_queue(window):
    _punch(window)
    window._refresh_punch_count()

    assert "1 attendance record today" in window.main_view.attendance_count_label.text()


# ── FACE_ATTENDANCE_ENABLED=false ─────────────────────────────────────


def test_disabled_attendance_keeps_the_single_column_gate_screen(
    qapp, tmp_path, monkeypatch
):
    """The configuration every gate PC without a working dlib build runs. It is
    a first-class layout, not a panel hidden behind dead space."""
    w = _make_window(qapp, tmp_path, monkeypatch, attendance_enabled=False)
    try:
        assert w.attendance_enabled is False
        assert w.main_view.gate_sidebar is None      # no sidebar wrapper
        assert w.face_service is None                # no webcam thread object
        # The whole gate flow is still there.
        assert w.main_view.allow_button is not None
        assert w.main_view.events_table is not None
    finally:
        w.close()


def test_disabled_attendance_makes_no_notice_and_still_decides(
    qapp, tmp_path, monkeypatch
):
    w = _make_window(qapp, tmp_path, monkeypatch, attendance_enabled=False)
    try:
        _enrol_staff(w)

        _decide(w, "ALLOW")

        assert w.speaker.said == []
        assert w.barrier.signals == 1                # the barrier still signals
        assert len(_events(w)) == 1
    finally:
        w.close()


def test_disabled_attendance_starts_no_face_thread(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path, monkeypatch, attendance_enabled=False)
    try:
        w._start_face_service()                      # must be a no-op
        assert w.face_service is None
    finally:
        w.close()


# ── Staff enrolment visibility ────────────────────────────────────────


def test_the_enrolment_strip_reports_a_roster_with_no_photos(window):
    """The live portal case: the record syncs, no photos come with it, and
    recognition can never succeed. The strip has to say so — otherwise it only
    ever shows up as a camera that recognises nobody."""
    window.staff_repo.upsert_staff(STAFF_UID, STAFF_NAME, 1, 1)

    window._refresh_enrolment_status()

    text = window.main_view.enrolment_label.text()
    assert "no photos" in text
    assert "portal" in text


def test_the_enrolment_strip_reports_a_healthy_roster(window):
    window.staff_repo.upsert_staff(STAFF_UID, STAFF_NAME, 1, 1)
    window.staff_repo.upsert_photo(STAFF_UID, 1, "h1", b"\x00" * 1024, 100)

    window._refresh_enrolment_status()

    assert "1 staff ready" in window.main_view.enrolment_label.text()


def test_the_enrolment_strip_says_nothing_has_synced_yet(window):
    window._refresh_enrolment_status()

    assert "No staff synced" in window.main_view.enrolment_label.text()


def test_opening_the_staff_dialog_shows_the_current_roster(window):
    window.staff_repo.upsert_staff(STAFF_UID, STAFF_NAME, 1, 1)

    window._open_staff_enrolment()

    dialog = window._enrolment_dialog
    assert dialog is not None
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 0).text() == STAFF_NAME
    # Plates-only is a legitimate roster entry now (membership is >=1 photo OR
    # >=1 plate), so it reads as a state, not a fault.
    assert dialog.table.item(0, 4).text() == "Plates only — no face enrolment"
    dialog.close()


def test_the_dialog_is_reused_rather_than_stacked(window):
    window._open_staff_enrolment()
    first = window._enrolment_dialog
    window._open_staff_enrolment()

    assert window._enrolment_dialog is first
    first.close()


def test_enrolment_refresh_is_inert_when_attendance_is_disabled(
    qapp, tmp_path, monkeypatch
):
    w = _make_window(qapp, tmp_path, monkeypatch, attendance_enabled=False)
    try:
        w._refresh_enrolment_status()          # must not raise
    finally:
        w.close()
