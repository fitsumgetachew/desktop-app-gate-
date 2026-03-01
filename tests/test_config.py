from pathlib import Path

from smart_gate.utils.config import load_config


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
