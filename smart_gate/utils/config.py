from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from smart_gate.utils.paths import get_app_data_dir, get_default_evidence_dir


DEFAULT_ENDPOINTS = {
    "AUTH_LOGIN": "/auth/login",
    "DEVICES_REGISTER": "/devices/register",
    "SYNC_ALLOWLIST": "/sync/allowlist",
    "SYNC_MANUAL_REASONS": "/sync/manual-reasons",
    "EVENTS": "/events",
    "DEVICES_HEARTBEAT": "/devices/heartbeat",
    "VEHICLES_LOOKUP": "/vehicles/lookup",
    "VEHICLES_REGISTER": "/vehicles/register",
}


@dataclass
class AppConfig:
    api_base_url: str
    env_mode: str
    gate_id: str
    lane_id: str
    direction: str
    camera_mode: str
    camera_index: int
    camera_rtsp_url: str
    evidence_dir: str
    sync_interval_seconds: int
    device_name: str
    endpoints: Dict[str, str]
    config_path: Path


def _parse_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = value.strip().strip("\"")
    return data


def _load_config_path() -> Path:
    env_path = os.environ.get("APP_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    default_path = get_default_config_path()
    return default_path


def get_default_config_path() -> Path:
    from smart_gate.utils.paths import get_app_data_dir, ensure_dir

    config_dir = ensure_dir(get_app_data_dir() / "config")
    return config_dir / "app.env"


def load_config() -> AppConfig:
    config_path = _load_config_path()
    file_data = _parse_env_file(config_path)

    def get(name: str, default: str) -> str:
        return os.environ.get(name, file_data.get(name, default))

    endpoints = {
        "AUTH_LOGIN": get("AUTH_ENDPOINT", DEFAULT_ENDPOINTS["AUTH_LOGIN"]),
        "DEVICES_REGISTER": get("DEVICES_REGISTER_ENDPOINT", DEFAULT_ENDPOINTS["DEVICES_REGISTER"]),
        "SYNC_ALLOWLIST": get("SYNC_ALLOWLIST_ENDPOINT", DEFAULT_ENDPOINTS["SYNC_ALLOWLIST"]),
        "SYNC_MANUAL_REASONS": get("SYNC_MANUAL_REASONS_ENDPOINT", DEFAULT_ENDPOINTS["SYNC_MANUAL_REASONS"]),
        "EVENTS": get("EVENTS_ENDPOINT", DEFAULT_ENDPOINTS["EVENTS"]),
        "DEVICES_HEARTBEAT": get("DEVICES_HEARTBEAT_ENDPOINT", DEFAULT_ENDPOINTS["DEVICES_HEARTBEAT"]),
        "VEHICLES_LOOKUP": get("VEHICLES_LOOKUP_ENDPOINT", DEFAULT_ENDPOINTS["VEHICLES_LOOKUP"]),
        "VEHICLES_REGISTER": get("VEHICLES_REGISTER_ENDPOINT", DEFAULT_ENDPOINTS["VEHICLES_REGISTER"]),
    }

    evidence_dir = get("EVIDENCE_DIR", "").strip()
    if not evidence_dir:
        evidence_dir = str(get_default_evidence_dir())
    else:
        path = Path(evidence_dir)
        if not path.is_absolute():
            evidence_dir = str(get_app_data_dir() / path)

    return AppConfig(
        api_base_url=get("API_BASE_URL", "http://localhost:8000"),
        env_mode=get("ENV_MODE", "DEV"),
        gate_id=get("GATE_ID", "GATE-1"),
        lane_id=get("LANE_ID", "LANE-A"),
        direction=get("DIRECTION", "ENTRY"),
        camera_mode=get("CAMERA_MODE", "USB"),
        camera_index=int(get("CAMERA_INDEX", "0")),
        camera_rtsp_url=get("CAMERA_RTSP_URL", ""),
        evidence_dir=evidence_dir,
        sync_interval_seconds=int(get("SYNC_INTERVAL_SECONDS", "10")),
        device_name=get("DEVICE_NAME", ""),
        endpoints=endpoints,
        config_path=config_path,
    )


def save_config(config: AppConfig) -> None:
    lines = [
        f"API_BASE_URL={config.api_base_url}",
        f"ENV_MODE={config.env_mode}",
        f"GATE_ID={config.gate_id}",
        f"LANE_ID={config.lane_id}",
        f"DIRECTION={config.direction}",
        f"CAMERA_MODE={config.camera_mode}",
        f"CAMERA_INDEX={config.camera_index}",
        f"CAMERA_RTSP_URL={config.camera_rtsp_url}",
        f"EVIDENCE_DIR={config.evidence_dir}",
        f"SYNC_INTERVAL_SECONDS={config.sync_interval_seconds}",
        f"DEVICE_NAME={config.device_name}",
        f"AUTH_ENDPOINT={config.endpoints['AUTH_LOGIN']}",
        f"DEVICES_REGISTER_ENDPOINT={config.endpoints['DEVICES_REGISTER']}",
        f"SYNC_ALLOWLIST_ENDPOINT={config.endpoints['SYNC_ALLOWLIST']}",
        f"SYNC_MANUAL_REASONS_ENDPOINT={config.endpoints['SYNC_MANUAL_REASONS']}",
        f"EVENTS_ENDPOINT={config.endpoints['EVENTS']}",
        f"DEVICES_HEARTBEAT_ENDPOINT={config.endpoints['DEVICES_HEARTBEAT']}",
        f"VEHICLES_LOOKUP_ENDPOINT={config.endpoints['VEHICLES_LOOKUP']}",
        f"VEHICLES_REGISTER_ENDPOINT={config.endpoints['VEHICLES_REGISTER']}",
    ]
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
