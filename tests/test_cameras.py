"""Configured camera sources: roles, serialisation, and the legacy upgrade.

Pure data — no OpenCV, no Qt. The rules worth pinning are the ones an operator
can hit from the settings page: assigning a role must take it off whoever had
it, a malformed config file must not stop the app, and a station upgrading from
the old two-camera settings must end up running exactly what it ran before.
"""

import pytest

from smart_gate.utils.cameras import (
    KIND_RTSP,
    KIND_USB,
    ROLE_FACE,
    ROLE_PLATE,
    ROLE_UNUSED,
    CameraSource,
    assign_role,
    camera_for_role,
    cameras_from_json,
    cameras_to_json,
    default_cameras,
    dedupe_ids,
    mask_url,
    new_camera_id,
)

RTSP_URL = "rtsp://admin:Passw0rd@192.168.1.64:554/Streaming/Channels/102"


def _usb(id_="c1", index=0, role=ROLE_UNUSED):
    return CameraSource(id_, "Webcam", KIND_USB, index, "", role)


def _rtsp(id_="c2", url=RTSP_URL, role=ROLE_UNUSED):
    return CameraSource(id_, "Lane camera", KIND_RTSP, 0, url, role)


# ── Credentials never reach a label ───────────────────────────────────


def test_the_rtsp_password_is_masked_for_display():
    """Hikvision URLs carry admin:<password> inline, and this string goes on a
    screen in a guard booth."""
    masked = mask_url(RTSP_URL)

    assert "Passw0rd" not in masked
    assert "admin" not in masked
    assert masked.endswith("/Streaming/Channels/102")


def test_a_url_without_credentials_is_left_alone():
    assert mask_url("rtsp://192.168.1.64:554/stream") == "rtsp://192.168.1.64:554/stream"


def test_masking_an_empty_url_is_safe():
    assert mask_url("") == ""


def test_the_location_label_never_leaks_the_password():
    assert "Passw0rd" not in _rtsp().location


def test_a_usb_location_names_the_device():
    assert _usb(index=3).location == "USB device 3"


def test_an_ip_camera_with_no_url_says_so_rather_than_looking_configured():
    assert _rtsp(url="").location == "(no URL set)"
    assert _rtsp(url="").configured is False


# ── Role assignment ───────────────────────────────────────────────────


def test_assigning_a_role_takes_it_from_the_previous_holder():
    """The app runs one ALPR pipeline and one face pipeline. Two cameras both
    claiming 'plate' would leave the choice to list order — a setting that
    silently does nothing is worse than one that is not offered."""
    cameras = [_usb("a", role=ROLE_PLATE), _usb("b", index=1)]

    updated = assign_role(cameras, "b", ROLE_PLATE)

    assert [c.role for c in updated] == [ROLE_UNUSED, ROLE_PLATE]


def test_assigning_a_different_role_leaves_others_alone():
    cameras = [_usb("a", role=ROLE_PLATE), _usb("b", index=1)]

    updated = assign_role(cameras, "b", ROLE_FACE)

    assert [c.role for c in updated] == [ROLE_PLATE, ROLE_FACE]


def test_unassigning_does_not_disturb_anyone_else():
    cameras = [_usb("a", role=ROLE_PLATE), _usb("b", index=1, role=ROLE_FACE)]

    updated = assign_role(cameras, "a", ROLE_UNUSED)

    assert [c.role for c in updated] == [ROLE_UNUSED, ROLE_FACE]


def test_an_unknown_role_falls_back_to_unused():
    updated = assign_role([_usb("a", role=ROLE_PLATE)], "a", "nonsense")

    assert updated[0].role == ROLE_UNUSED


def test_camera_for_role_finds_the_assigned_one():
    cameras = [_usb("a"), _usb("b", index=1, role=ROLE_FACE)]

    assert camera_for_role(cameras, ROLE_FACE).id == "b"
    assert camera_for_role(cameras, ROLE_PLATE) is None


def test_an_unconfigured_camera_does_not_count_as_holding_its_role():
    """An IP camera that has not been given a URL yet is listed but cannot run,
    and must not shadow a working one."""
    cameras = [_rtsp("a", url="", role=ROLE_PLATE)]

    assert camera_for_role(cameras, ROLE_PLATE) is None


# ── Serialisation ─────────────────────────────────────────────────────


def test_the_json_form_is_a_single_line():
    """It has to live in a .env file, one key per line."""
    payload = cameras_to_json([_usb(), _rtsp()])

    assert "\n" not in payload


def test_cameras_round_trip_through_json():
    original = [_usb("a", 2, ROLE_FACE), _rtsp("b", RTSP_URL, ROLE_PLATE)]

    restored = cameras_from_json(cameras_to_json(original))

    assert [c.to_dict() for c in restored] == [c.to_dict() for c in original]


@pytest.mark.parametrize("raw", ["", "   ", "{not json", "null", '{"a": 1}', "[1, 2]"])
def test_malformed_json_yields_no_cameras_rather_than_raising(raw):
    """This file is hand-editable on a gate PC; one bad character must not stop
    the app from starting."""
    assert cameras_from_json(raw) == [] or all(
        isinstance(c, CameraSource) for c in cameras_from_json(raw)
    )


def test_an_entry_with_a_bad_kind_or_role_is_repaired_not_dropped():
    restored = cameras_from_json(
        '[{"id":"x","name":"Cam","kind":"MAGIC","index":"nope","role":"boss"}]'
    )

    assert len(restored) == 1
    assert restored[0].kind == KIND_USB
    assert restored[0].role == ROLE_UNUSED
    assert restored[0].index == 0


def test_duplicate_ids_are_given_fresh_ones():
    """Ids address rows in the settings page; two rows sharing one would make
    removing a camera delete the wrong entry."""
    cameras = dedupe_ids([_usb("same"), _usb("same", index=1)])

    assert cameras[0].id != cameras[1].id


def test_new_ids_are_unique():
    assert new_camera_id() != new_camera_id()


# ── Upgrading from the single-camera settings ─────────────────────────


def test_the_legacy_usb_pair_is_reproduced_exactly():
    """Every station upgrading lands here; the result must be what it was
    already running."""
    cameras = default_cameras("USB", 1, "", 2)

    plate = camera_for_role(cameras, ROLE_PLATE)
    face = camera_for_role(cameras, ROLE_FACE)
    assert (plate.kind, plate.index) == (KIND_USB, 1)
    assert (face.kind, face.index) == (KIND_USB, 2)


def test_a_legacy_rtsp_lane_camera_keeps_its_url():
    cameras = default_cameras("RTSP", 0, RTSP_URL, 0)

    plate = camera_for_role(cameras, ROLE_PLATE)
    assert plate.kind == KIND_RTSP
    assert plate.url == RTSP_URL


def test_an_unknown_legacy_mode_is_treated_as_usb():
    cameras = default_cameras("carrier-pigeon", 0, "", 0)

    assert camera_for_role(cameras, ROLE_PLATE).kind == KIND_USB
