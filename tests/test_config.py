from pathlib import Path

import pytest

from smart_gate.utils.cameras import (
    ROLE_FACE,
    ROLE_PLATE,
    ROLE_UNUSED,
    CameraSource,
    camera_for_role,
)
from smart_gate.utils.config import (
    DEFAULT_ENDPOINTS,
    ENDPOINT_ENV_VARS,
    load_config,
    save_config,
)


def test_load_config_from_env_file(monkeypatch, tmp_path: Path):
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "API_BASE_URL=http://example.com\n"
        "GATE_ID=G1\n"
        "LANE_ID=L1\n"
        "DIRECTION=EXIT\n"
        "CAMERA_MODE=RTSP\n"
        "CAMERA_INDEX=2\n"
        "CAMERA_RTSP_URL=rtsp://test\n"
        "SYNC_INTERVAL_SECONDS=15\n"
        "ENV_MODE=PROD\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()
    assert config.api_base_url == "http://example.com"
    assert config.gate_id == "G1"
    assert config.lane_id == "L1"
    assert config.direction == "EXIT"
    assert config.camera_mode == "RTSP"
    assert config.camera_index == 2
    assert config.camera_rtsp_url == "rtsp://test"
    assert config.sync_interval_seconds == 15
    assert config.env_mode == "PROD"


def test_save_config_persists_every_endpoint_override(monkeypatch, tmp_path: Path):
    """save_config used to write back only 8 of the endpoint keys, silently
    resetting AUTH_DESKTOP_*/AUTH_REFRESH/DEVICES_CHECK/EVENTS_BATCH on the
    first Settings save."""
    env_path = tmp_path / "app.env"
    overrides = {
        env_var: f"/custom{DEFAULT_ENDPOINTS[key]}"
        for key, env_var in ENDPOINT_ENV_VARS.items()
    }
    env_path.write_text(
        "API_BASE_URL=http://example.com\n"
        + "".join(f"{k}={v}\n" for k, v in overrides.items())
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()
    assert config.endpoints["AUTH_DESKTOP_START"] == "/custom/auth/desktop/start"

    save_config(config)
    reloaded = load_config()

    assert reloaded.endpoints == config.endpoints
    for key in ENDPOINT_ENV_VARS:
        assert reloaded.endpoints[key] == f"/custom{DEFAULT_ENDPOINTS[key]}"


def test_endpoint_env_vars_cover_every_default():
    assert set(ENDPOINT_ENV_VARS) == set(DEFAULT_ENDPOINTS)


def test_bad_int_values_fall_back_to_defaults(monkeypatch, tmp_path: Path):
    """A typo in the config must not stop the gate from starting."""
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "CAMERA_INDEX=not-a-number\nSYNC_INTERVAL_SECONDS=\nAUTO_ALLOW_SECONDS=soon\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()

    assert config.camera_index == 0
    assert config.sync_interval_seconds == 10
    assert config.auto_allow_seconds == 5


def test_auto_allow_seconds_round_trips(monkeypatch, tmp_path: Path):
    env_path = tmp_path / "app.env"
    env_path.write_text("AUTO_ALLOW_SECONDS=8\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()
    assert config.auto_allow_seconds == 8

    config.auto_allow_seconds = 0        # 0 disables auto-continue
    save_config(config)
    assert load_config().auto_allow_seconds == 0


def test_negative_auto_allow_seconds_is_clamped_to_disabled(monkeypatch, tmp_path: Path):
    env_path = tmp_path / "app.env"
    env_path.write_text("AUTO_ALLOW_SECONDS=-3\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    assert load_config().auto_allow_seconds == 0


# ── Staff face attendance ─────────────────────────────────────────────


def test_face_attendance_defaults_match_the_reference_implementation(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))

    config = load_config()

    assert config.face_attendance_enabled is True
    assert config.face_camera_index == 0
    assert config.face_max_fps == 3.0
    # verify_face_with_confidence's strict tolerance / min confidence.
    assert config.face_tolerance == 0.45
    assert config.face_min_confidence == 55.0


def test_face_settings_round_trip_through_save_config(monkeypatch, tmp_path: Path):
    """save_config has historically dropped keys it did not know about, silently
    reverting overrides on the first Settings save."""
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "FACE_ATTENDANCE_ENABLED=false\n"
        "FACE_CAMERA_INDEX=2\n"
        "FACE_MAX_FPS=2\n"
        "FACE_TOLERANCE=0.4\n"
        "FACE_MIN_CONFIDENCE=60\n"
        "SYNC_STAFF_ROSTER_ENDPOINT=/custom/staff\n"
        "ATTENDANCE_BATCH_ENDPOINT=/custom/attendance\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()
    save_config(config)
    reloaded = load_config()

    assert reloaded == config
    assert reloaded.face_attendance_enabled is False
    assert reloaded.face_camera_index == 2
    assert reloaded.face_max_fps == 2.0
    assert reloaded.face_tolerance == 0.4
    assert reloaded.face_min_confidence == 60.0
    assert reloaded.endpoints["SYNC_STAFF_ROSTER"] == "/custom/staff"
    assert reloaded.endpoints["ATTENDANCE_BATCH"] == "/custom/attendance"


def test_bad_face_values_fall_back_instead_of_crashing_startup(monkeypatch, tmp_path: Path):
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "FACE_ATTENDANCE_ENABLED=maybe\n"
        "FACE_CAMERA_INDEX=front\n"
        "FACE_MAX_FPS=fast\n"
        "FACE_TOLERANCE=loose\n"
        "FACE_MIN_CONFIDENCE=\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()

    assert config.face_attendance_enabled is True
    assert config.face_camera_index == 0
    assert config.face_max_fps == 3.0
    assert config.face_tolerance == 0.45
    assert config.face_min_confidence == 55.0


def test_out_of_range_face_values_are_clamped(monkeypatch, tmp_path: Path):
    """A tolerance of 9.9 would match every face against every staff member."""
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "FACE_CAMERA_INDEX=-1\n"
        "FACE_MAX_FPS=500\n"
        "FACE_TOLERANCE=9.9\n"
        "FACE_MIN_CONFIDENCE=250\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()

    assert config.face_camera_index == 0
    assert config.face_max_fps == 15.0
    assert config.face_tolerance == 1.0
    assert config.face_min_confidence == 100.0


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("on", True),
     ("false", False), ("0", False), ("no", False), ("off", False), ("OFF", False)],
)
def test_face_attendance_enabled_accepts_the_usual_spellings(
    monkeypatch, tmp_path: Path, raw, expected
):
    env_path = tmp_path / f"app-{raw}.env"
    env_path.write_text(f"FACE_ATTENDANCE_ENABLED={raw}\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    assert load_config().face_attendance_enabled is expected


# ── Camera sources ────────────────────────────────────────────────────


def test_a_config_without_the_cameras_key_derives_them_from_the_old_settings(
    monkeypatch, tmp_path: Path
):
    """Every station upgrading from the single-camera settings lands here. The
    upgrade must be a no-op: it has to keep running the cameras it was running."""
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "CAMERA_MODE=RTSP\nCAMERA_RTSP_URL=rtsp://cam/1\nCAMERA_INDEX=0\n"
        "FACE_CAMERA_INDEX=2\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()

    plate = camera_for_role(config.cameras, ROLE_PLATE)
    face = camera_for_role(config.cameras, ROLE_FACE)
    assert plate.kind == "RTSP" and plate.url == "rtsp://cam/1"
    assert face.index == 2
    # And the flat fields the services read are untouched.
    assert config.camera_mode == "RTSP"
    assert config.camera_rtsp_url == "rtsp://cam/1"
    assert config.face_camera_index == 2


def test_the_camera_list_round_trips_and_drives_the_flat_fields(
    monkeypatch, tmp_path: Path
):
    """camera_service and face_camera_service read the flat fields, so a list
    that disagreed with them would show one camera and open another."""
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    config = load_config()
    config.cameras = [
        CameraSource("c1", "Lane", "RTSP", 0, "rtsp://lane/1", ROLE_PLATE),
        CameraSource("c2", "Booth", "USB", 4, "", ROLE_FACE),
    ]

    save_config(config)
    reloaded = load_config()

    assert [c.id for c in reloaded.cameras] == ["c1", "c2"]
    assert reloaded.camera_mode == "RTSP"
    assert reloaded.camera_rtsp_url == "rtsp://lane/1"
    assert reloaded.face_camera_index == 4


def test_a_corrupt_cameras_line_falls_back_instead_of_crashing(
    monkeypatch, tmp_path: Path
):
    """The file is hand-editable on a gate PC."""
    env_path = tmp_path / "app.env"
    env_path.write_text("CAMERA_INDEX=1\nFACE_CAMERA_INDEX=2\nCAMERAS=[{oops\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()

    assert len(config.cameras) == 2            # rebuilt from the legacy keys
    assert config.camera_index == 1
    assert config.face_camera_index == 2


def test_the_cameras_line_never_wraps(monkeypatch, tmp_path: Path):
    """One key per line is the whole format; a wrapped value would be
    unparseable on the next load."""
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    config = load_config()
    config.cameras = [
        CameraSource(f"c{i}", f"Camera {i}", "USB", i, "", ROLE_UNUSED)
        for i in range(6)
    ]

    save_config(config)

    camera_lines = [
        line
        for line in (tmp_path / "app.env").read_text().splitlines()
        if line.startswith("CAMERAS=")
    ]
    assert len(camera_lines) == 1
    assert load_config().cameras[3].name == "Camera 3"
