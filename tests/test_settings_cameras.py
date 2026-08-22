"""The camera list in the settings page.

Driven on a real ``SettingsPage`` offscreen, because the behaviour worth testing
is the interaction between rows — assigning a role has to take it off whichever
other row held it, and that lives in the widget wiring, not in a pure function.

No camera is ever opened: the scan results are handed in directly, the same
shape ``DiscoveryWorker`` emits.
"""

import pytest
from PySide6 import QtWidgets

from smart_gate.services.camera_discovery import DiscoveredCamera
from smart_gate.ui.settings_view import SettingsPage
from smart_gate.utils.cameras import (
    KIND_RTSP,
    KIND_USB,
    ROLE_FACE,
    ROLE_PLATE,
    ROLE_UNUSED,
    CameraSource,
)
from smart_gate.utils.config import load_config

RTSP_URL = "rtsp://admin:Passw0rd@192.168.1.64:554/Streaming/Channels/102"


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    cfg = load_config()
    cfg.cameras = [
        CameraSource("c1", "Lane camera", KIND_RTSP, 0, RTSP_URL, ROLE_PLATE),
        CameraSource("c2", "Booth webcam", KIND_USB, 0, "", ROLE_FACE),
    ]
    return cfg


@pytest.fixture
def page(qapp, config):
    p = SettingsPage()
    p.load_from_config(config)
    yield p
    p.deleteLater()


def _roles(page):
    return [row.role for row in page._camera_rows]


def _ids(page):
    return [row.camera_id for row in page._camera_rows]


# ── Loading ───────────────────────────────────────────────────────────


def test_the_configured_cameras_become_rows(page):
    assert _ids(page) == ["c1", "c2"]
    assert _roles(page) == [ROLE_PLATE, ROLE_FACE]


def test_reloading_replaces_the_rows_rather_than_appending(page, config):
    page.load_from_config(config)

    assert len(page._camera_rows) == 2


def test_a_station_with_no_cameras_shows_the_empty_state(page, config):
    config.cameras = []

    page.load_from_config(config)

    assert page._camera_rows == []
    assert not page.no_cameras_label.isHidden()


def test_the_placeholder_disappears_once_a_camera_exists(page):
    assert page.no_cameras_label.isHidden()


# ── Editing the row shape ─────────────────────────────────────────────


def test_an_ip_camera_shows_a_url_field_and_no_device_number(page):
    row = page._camera_rows[0]

    assert not row.url_input.isHidden()
    assert row.index_spin.isHidden()


def test_a_usb_camera_shows_a_device_number_and_no_url(page):
    row = page._camera_rows[1]

    assert row.url_input.isHidden()
    assert not row.index_spin.isHidden()


def test_switching_the_type_swaps_the_editor(page):
    """One field that means two things would be worse than swapping them."""
    row = page._camera_rows[1]

    row.kind_combo.setCurrentText(KIND_RTSP)

    assert not row.url_input.isHidden()
    assert row.index_spin.isHidden()


# ── Role exclusivity ──────────────────────────────────────────────────


def test_giving_a_role_to_one_row_takes_it_from_the_other(page):
    """Only one ALPR pipeline and one face pipeline exist to run."""
    page._camera_rows[1].role_combo.setCurrentIndex(list(  # -> plate
        page._camera_rows[1].role_combo.itemData(i) for i in range(3)
    ).index(ROLE_PLATE))

    assert _roles(page) == [ROLE_UNUSED, ROLE_PLATE]


def test_setting_a_row_to_unused_leaves_the_others_alone(page):
    page._on_role_changed("c1", ROLE_UNUSED)

    assert _roles(page) == [ROLE_PLATE, ROLE_FACE]


def test_a_role_taken_away_is_reflected_in_the_other_rows_combo(page):
    page._on_role_changed("c2", ROLE_PLATE)

    assert page._camera_rows[0].role_combo.currentData() == ROLE_UNUSED


# ── Adding and removing ───────────────────────────────────────────────


def test_adding_a_usb_camera_picks_a_free_device_number(page):
    """Defaulting every new camera to device 0 would collide with the webcam
    that is already there."""
    row = page._add_camera(KIND_USB)

    assert row.to_camera().index != 0


def test_adding_a_camera_suggests_a_job_nothing_is_doing(page, config):
    config.cameras = [CameraSource("only", "Webcam", KIND_USB, 0, "", ROLE_PLATE)]
    page.load_from_config(config)

    row = page._add_camera(KIND_USB)

    assert row.role == ROLE_FACE


def test_adding_a_camera_when_both_jobs_are_taken_leaves_it_unassigned(page):
    row = page._add_camera(KIND_USB)

    assert row.role == ROLE_UNUSED
    assert _roles(page)[:2] == [ROLE_PLATE, ROLE_FACE]   # nothing was stolen


def test_an_added_ip_camera_starts_empty_and_unconfigured(page):
    row = page._add_camera(KIND_RTSP)

    assert row.to_camera().kind == KIND_RTSP
    assert row.to_camera().configured is False


def test_removing_a_row_removes_only_that_camera(page):
    page._on_remove_requested("c1")

    assert _ids(page) == ["c2"]


def test_removing_the_last_camera_restores_the_empty_state(page):
    page._on_remove_requested("c1")
    page._on_remove_requested("c2")

    assert page._camera_rows == []
    assert not page.no_cameras_label.isHidden()


# ── Scanning ──────────────────────────────────────────────────────────


def test_a_scan_adds_only_cameras_that_are_not_already_configured(page):
    """The booth webcam is already device 0; re-adding it would give the
    operator two rows fighting over one device."""
    page._on_scan_finished([DiscoveredCamera(0, 640, 480), DiscoveredCamera(2, 1280, 720)])

    indices = [r.to_camera().index for r in page._camera_rows if r.to_camera().kind == KIND_USB]
    assert sorted(indices) == [0, 2]
    assert "added 1 new" in page.camera_scan_status.text()


def test_a_scan_that_finds_nothing_says_so(page):
    page._on_scan_finished([])

    assert "No USB cameras found" in page.camera_scan_status.text()
    assert len(page._camera_rows) == 2


def test_a_scan_finding_only_known_cameras_says_nothing_was_added(page):
    page._on_scan_finished([DiscoveredCamera(0, 640, 480)])

    assert "already configured" in page.camera_scan_status.text()
    assert len(page._camera_rows) == 2


def test_a_scan_never_disturbs_an_existing_assignment(page):
    page._on_scan_finished([DiscoveredCamera(5, 640, 480)])

    assert _roles(page)[:2] == [ROLE_PLATE, ROLE_FACE]


def test_a_failed_scan_reports_the_reason_without_changing_anything(page):
    page._on_scan_failed("ConnectionError")

    assert "Camera scan failed" in page.camera_scan_status.text()
    assert len(page._camera_rows) == 2


# ── Saving ────────────────────────────────────────────────────────────


def test_saving_writes_the_rows_back_and_drives_the_flat_fields(page, config):
    """The flat fields are what camera_service actually opens."""
    page._camera_rows[1].index_spin.setValue(3)
    saved = []
    page.settings_saved.connect(saved.append)

    page._save()

    assert saved
    result = saved[0]
    assert [c.id for c in result.cameras] == ["c1", "c2"]
    assert result.camera_mode == KIND_RTSP
    assert result.camera_rtsp_url == RTSP_URL
    assert result.face_camera_index == 3


def test_saving_carries_the_attendance_settings(page):
    page.face_attendance_enabled.setChecked(False)
    page.face_tolerance.setValue(0.40)
    saved = []
    page.settings_saved.connect(saved.append)

    page._save()

    assert saved[0].face_attendance_enabled is False
    assert saved[0].face_tolerance == pytest.approx(0.40)


def test_a_row_left_unnamed_still_saves_with_something_readable(page):
    page._camera_rows[0].name_input.setText("   ")
    saved = []
    page.settings_saved.connect(saved.append)

    page._save()

    assert saved[0].cameras[0].name == "Camera"


# ── Unassigned roles must not fail silently ───────────────────────────


def test_no_warning_when_both_jobs_are_assigned(page):
    assert page.camera_role_warning.isHidden()


def test_unassigning_a_role_warns_that_the_setting_will_not_take_effect(page):
    """The services read the flat camera_* fields, which keep their last value
    when nothing holds the role — so the gate carries on using the old camera
    while this page says 'Not used'. Fail-safe, but silent."""
    page._camera_rows[0].set_role_silently(ROLE_UNUSED)
    page._refresh_role_warning()

    assert not page.camera_role_warning.isHidden()
    assert "Plate / ALPR" in page.camera_role_warning.text()


def test_both_roles_unassigned_names_both(page):
    for row in page._camera_rows:
        row.set_role_silently(ROLE_UNUSED)
    page._refresh_role_warning()

    text = page.camera_role_warning.text()
    assert "Plate / ALPR" in text and "Face / Attendance" in text


def test_reassigning_clears_the_warning(page):
    page._camera_rows[0].set_role_silently(ROLE_UNUSED)
    page._refresh_role_warning()
    assert not page.camera_role_warning.isHidden()

    page._camera_rows[0].set_role_silently(ROLE_PLATE)
    page._refresh_role_warning()

    assert page.camera_role_warning.isHidden()


def test_an_empty_camera_list_shows_no_role_warning(page, config):
    """Nothing configured is a different message — the placeholder covers it."""
    config.cameras = []
    page.load_from_config(config)

    assert page.camera_role_warning.isHidden()
