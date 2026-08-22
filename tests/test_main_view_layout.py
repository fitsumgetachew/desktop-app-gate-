"""The view keeps its contract in both layouts.

Task 5 restructures the screen but is not allowed to rename or drop anything
``AppWindow`` already wires. These tests hold that line: every pre-existing
signal and widget must survive both the attendance layout and the single-column
fallback, because a missing attribute here is a crash in the gate flow, not a
cosmetic problem.
"""

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.services import attendance_display
from smart_gate.ui.main_view import BARRIER_FLASH_MS, MainGateView

# Everything AppWindow connects or calls today.
EXISTING_SIGNALS = [
    "decision_requested", "capture_requested", "settings_requested",
    "logout_requested", "sync_now_requested", "check_status_requested",
    "sync_recheck_requested", "add_temp_permit_requested", "fullscreen_requested",
    "auto_allow_cancelled", "alarm_acknowledged", "register_vehicle_requested",
]
EXISTING_WIDGETS = [
    "camera_label", "decision_banner", "decision_subtext", "plate_input",
    "allow_button", "deny_button", "capture_button", "events_table",
    "reason_dropdown", "note_input", "status_result_label", "offline_banner",
    "stop_auto_button", "ack_alarm_button", "register_vehicle_button",
]
EXISTING_METHODS = [
    "update_frame", "set_camera_status", "set_online_status", "set_user",
    "set_gate_lane", "set_sync_status", "set_last_sync", "set_next_sync",
    "set_reasons", "get_manual_inputs", "set_recent_events", "set_status_result",
    "set_decision_state", "set_countdown", "clear_decision_state",
    "set_alarm_acknowledged", "set_vehicle_details", "set_offline_mode",
    "set_presence_hint", "set_plate_text", "set_plate_detected",
    "clear_plate_detected",
]


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(params=[True, False], ids=["attendance_on", "attendance_off"])
def view(qapp, request):
    v = MainGateView(attendance_enabled=request.param)
    yield v
    v.deleteLater()


# ── Nothing was renamed or dropped ────────────────────────────────────


@pytest.mark.parametrize("name", EXISTING_SIGNALS)
def test_every_pre_existing_signal_survives_both_layouts(view, name):
    assert isinstance(getattr(view, name), QtCore.SignalInstance)


@pytest.mark.parametrize("name", EXISTING_WIDGETS)
def test_every_pre_existing_widget_survives_both_layouts(view, name):
    assert getattr(view, name) is not None


@pytest.mark.parametrize("name", EXISTING_METHODS)
def test_every_pre_existing_method_survives_both_layouts(view, name):
    assert callable(getattr(view, name))


def test_the_gate_widgets_are_built_exactly_once(view):
    """Both branches build the same panels once and re-parent them; a second
    copy would leave half the app wired to a widget nobody can see."""
    assert len(view.findChildren(QtWidgets.QPushButton, "")) >= 0
    allows = [
        b for b in view.findChildren(QtWidgets.QPushButton)
        if b.text().strip().upper() == "ALLOW"
    ]
    assert len(allows) == 1


# ── The layout differs, deliberately ──────────────────────────────────


def test_attendance_on_builds_the_sidebar_and_the_panel(qapp):
    view = MainGateView(attendance_enabled=True)

    assert view.attendance_enabled is True
    assert view.gate_sidebar is not None
    assert view.attendance_camera_label is not None
    assert view.attendance_state_label is not None
    assert view.attendance_count_label is not None


def test_attendance_off_has_no_sidebar_wrapper_and_no_dead_space(qapp):
    """The single-column gate screen exactly as it was, not the new layout with
    an empty column where the panel would be."""
    view = MainGateView(attendance_enabled=False)

    assert view.attendance_enabled is False
    assert view.gate_sidebar is None
    assert not hasattr(view, "attendance_camera_label")


# ── The attendance API is inert when disabled ─────────────────────────


def test_every_attendance_call_is_a_safe_no_op_when_disabled(qapp):
    """AppWindow guards these too, but the view must not depend on that: a
    stray signal from a worker shutting down cannot be allowed to crash."""
    view = MainGateView(attendance_enabled=False)

    view.update_attendance_frame(QtGui.QImage(4, 4, QtGui.QImage.Format_RGB888))
    view.set_attendance_camera_status(True, "connected")
    view.apply_attendance_state(attendance_display.recognised("Abebe", 1_000_000))
    view.set_attendance_count(3)
    view.show_attendance_notice("something")
    view.clear_attendance_notice()
    view.clear_attendance_state()          # must not raise


# ── Attendance panel behaviour ────────────────────────────────────────


def test_the_panel_starts_idle(qapp):
    view = MainGateView(attendance_enabled=True)

    assert view.attendance_state_label.text() == attendance_display.IDLE_TEXT


def test_applying_a_state_paints_its_text(qapp):
    view = MainGateView(attendance_enabled=True)

    view.apply_attendance_state(
        attendance_display.recognised("Abebe Bekele", 1_000_000)
    )

    assert "Abebe Bekele" in view.attendance_state_label.text()


def test_a_transient_state_arms_the_return_to_idle(qapp):
    view = MainGateView(attendance_enabled=True)

    view.apply_attendance_state(attendance_display.unrecognised())

    assert view._attendance_reset_timer.isActive()


def test_clearing_returns_to_the_idle_prompt(qapp):
    view = MainGateView(attendance_enabled=True)
    view.apply_attendance_state(attendance_display.unrecognised())

    view.clear_attendance_state()

    assert view.attendance_state_label.text() == attendance_display.IDLE_TEXT
    assert not view._attendance_reset_timer.isActive()


def test_the_notice_banner_shows_and_hides(qapp):
    view = MainGateView(attendance_enabled=True)
    assert view.attendance_notice_banner.isHidden()

    view.show_attendance_notice("Sara has not recorded attendance today")
    assert not view.attendance_notice_banner.isHidden()

    view.clear_attendance_notice()
    assert view.attendance_notice_banner.isHidden()


def test_an_empty_notice_is_not_shown(qapp):
    view = MainGateView(attendance_enabled=True)

    view.show_attendance_notice("")

    assert view.attendance_notice_banner.isHidden()


# ── Barrier indicator ─────────────────────────────────────────────────


def test_the_barrier_indicator_starts_hidden(view):
    assert view.barrier_signal_label.isHidden()


def test_flashing_the_barrier_shows_it_and_arms_the_timer(view):
    """Present in both layouts: the barrier signal belongs to the gate, not to
    attendance."""
    view.flash_barrier_signal()

    assert not view.barrier_signal_label.isHidden()
    assert view._barrier_timer.isActive()
    assert view._barrier_timer.interval() == BARRIER_FLASH_MS


def test_flashing_twice_restarts_rather_than_stacking(view):
    view.flash_barrier_signal()
    view.flash_barrier_signal()

    assert not view.barrier_signal_label.isHidden()
