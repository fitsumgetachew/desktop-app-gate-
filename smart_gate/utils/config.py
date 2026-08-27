from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from smart_gate.utils.cameras import (
    ROLE_FACE,
    ROLE_PLATE,
    CameraSource,
    camera_for_role,
    cameras_from_json,
    cameras_to_json,
    default_cameras,
)
from smart_gate.utils.environment import environment_key, environment_label
from smart_gate.utils.paths import (
    get_app_data_dir,
    get_default_evidence_dir,
    set_active_environment,
)

logger = logging.getLogger(__name__)


DEFAULT_ENDPOINTS = {
    "AUTH_LOGIN": "/auth/login",
    "AUTH_DESKTOP_START": "/auth/desktop/start",
    "AUTH_DESKTOP_EXCHANGE": "/auth/desktop/exchange",
    "AUTH_REFRESH": "/auth/refresh",
    "AUTH_LOGOUT": "/auth/logout",
    "DEVICES_REGISTER": "/devices/register",
    "DEVICES_CHECK": "/devices/check",
    "DEVICES_HEARTBEAT": "/devices/heartbeat",
    "SYNC_ALLOWLIST": "/sync/allowlist",
    "SYNC_MANUAL_REASONS": "/sync/manual-reasons",
    "SYNC_STAFF_ROSTER": "/sync/staff-roster",
    "EVENTS": "/events",
    "EVENTS_BATCH": "/events/batch",
    "ATTENDANCE_BATCH": "/attendance/batch",
    "VEHICLES_LOOKUP": "/vehicles/lookup",
    "PERMITS_TEMPORARY": "/permits/temporary",
    "VEHICLES_REGISTER_VISITOR": "/vehicles/register-visitor",
}

# endpoint key → name of the .env variable that overrides it.  load_config and
# save_config both iterate this map, so a new endpoint can never be read but
# silently dropped on save again.
ENDPOINT_ENV_VARS = {
    "AUTH_LOGIN": "AUTH_ENDPOINT",
    "AUTH_DESKTOP_START": "AUTH_DESKTOP_START_ENDPOINT",
    "AUTH_DESKTOP_EXCHANGE": "AUTH_DESKTOP_EXCHANGE_ENDPOINT",
    "AUTH_REFRESH": "AUTH_REFRESH_ENDPOINT",
    "AUTH_LOGOUT": "AUTH_LOGOUT_ENDPOINT",
    "DEVICES_REGISTER": "DEVICES_REGISTER_ENDPOINT",
    "DEVICES_CHECK": "DEVICES_CHECK_ENDPOINT",
    "DEVICES_HEARTBEAT": "DEVICES_HEARTBEAT_ENDPOINT",
    "SYNC_ALLOWLIST": "SYNC_ALLOWLIST_ENDPOINT",
    "SYNC_MANUAL_REASONS": "SYNC_MANUAL_REASONS_ENDPOINT",
    "SYNC_STAFF_ROSTER": "SYNC_STAFF_ROSTER_ENDPOINT",
    "EVENTS": "EVENTS_ENDPOINT",
    "EVENTS_BATCH": "EVENTS_BATCH_ENDPOINT",
    "ATTENDANCE_BATCH": "ATTENDANCE_BATCH_ENDPOINT",
    "VEHICLES_LOOKUP": "VEHICLES_LOOKUP_ENDPOINT",
    "PERMITS_TEMPORARY": "PERMITS_TEMPORARY_ENDPOINT",
    "VEHICLES_REGISTER_VISITOR": "VEHICLES_REGISTER_VISITOR_ENDPOINT",
}


AUTH_MODE_MOCK = "mock"
AUTH_MODE_PORTAL = "portal"
AUTH_MODES = (AUTH_MODE_MOCK, AUTH_MODE_PORTAL)

DEFAULT_PORTAL_SSO_URL = "https://sit-portal-e6750.web.app/sso"


@dataclass
class AppConfig:
    api_base_url: str
    env_mode: str
    # "mock"   — the desktop collects email+password and drives /auth/desktop/start
    #            itself (reference server).
    # "portal" — the operator signs in on the SIT portal in a browser, the portal
    #            mints the one-time code and the desktop only does the exchange.
    #            No credential ever touches this app in portal mode.
    auth_mode: str
    portal_sso_url: str
    gate_id: str
    lane_id: str
    direction: str
    camera_mode: str
    camera_index: int
    camera_rtsp_url: str
    evidence_dir: str
    sync_interval_seconds: int
    device_name: str
    # Seconds the GREEN state counts down before auto-confirming ALLOW.
    # 0 disables auto-continue entirely (every decision stays manual).
    auto_allow_seconds: int
    # Plate read zone: "x,y,w,h" as fractions of the frame, empty = full frame.
    # A software zoom for a camera that watches a whole yard — see utils/roi.py.
    alpr_roi: str
    # Spoken output. TTS_VOICE is matched by substring against installed
    # voices (pyttsx3) or an spd-say voice type; see scripts/list_voices.py.
    tts_voice: str
    tts_rate: int
    tts_volume: float
    # ── Staff face attendance ────────────────────────────────
    # A station with no webcam, or one where the face stack failed to
    # install, sets face_attendance_enabled=false and behaves exactly like a
    # plain gate PC. The camera index is deliberately independent of
    # camera_index: the ALPR camera watches the lane, this one watches a
    # person standing at the window.
    face_attendance_enabled: bool
    face_camera_index: int
    face_max_fps: float
    face_tolerance: float
    face_min_confidence: float
    # Every camera this station knows about, each tagged with what it is
    # for. The scalar camera_* / face_camera_index fields above stay in
    # sync with whichever source holds each role, so the rest of the app
    # keeps reading exactly what it always read.
    cameras: List[CameraSource]
    endpoints: Dict[str, str]
    config_path: Path

    # Which server this station belongs to. Derived, never stored: the base
    # URL is the single source of truth, and a stored copy could drift.
    @property
    def environment_key(self) -> str:
        return environment_key(self.api_base_url)

    @property
    def environment_label(self) -> str:
        return environment_label(self.api_base_url)


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


def _parse_int(name: str, raw: str, default: int) -> int:
    """Parse an integer setting, falling back to the default on bad input.

    A typo in the config file must not stop the gate from starting.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s: %r — falling back to %s", name, raw, default
        )
        return default


def _parse_float(name: str, raw: str, default: float, low: float, high: float) -> float:
    """Parse a float setting and clamp it into ``[low, high]``.

    Same contract as :func:`_parse_int`: a typo must not stop the gate from
    starting. The clamp matters more here than for the integers — a tolerance of
    ``9.9`` would match every face against every staff member.
    """
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s: %r — falling back to %s", name, raw, default
        )
        return default
    clamped = max(low, min(high, value))
    if clamped != value:
        logger.warning(
            "%s=%s is outside [%s, %s] — clamped to %s", name, value, low, high, clamped
        )
    return clamped


def _parse_bool(name: str, raw: str, default: bool) -> bool:
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    if value:
        logger.warning(
            "Invalid value for %s: %r — falling back to %s", name, raw, default
        )
    return default


def _parse_auth_mode(raw: str) -> str:
    """Normalise AUTH_MODE, falling back to ``mock`` on anything unrecognised.

    A typo here must not lock the operator out with a half-configured screen.
    """
    mode = str(raw).strip().lower()
    if mode not in AUTH_MODES:
        if mode:
            logger.warning(
                "Unknown AUTH_MODE %r — falling back to %s", raw, AUTH_MODE_MOCK
            )
        return AUTH_MODE_MOCK
    return mode


def load_config() -> AppConfig:
    config_path = _load_config_path()
    file_data = _parse_env_file(config_path)

    def get(name: str, default: str) -> str:
        return os.environ.get(name, file_data.get(name, default))

    endpoints = {
        key: get(env_var, DEFAULT_ENDPOINTS[key])
        for key, env_var in ENDPOINT_ENV_VARS.items()
    }

    camera_mode = get("CAMERA_MODE", "USB")
    camera_index = _parse_int("CAMERA_INDEX", get("CAMERA_INDEX", "0"), 0)
    camera_rtsp_url = get("CAMERA_RTSP_URL", "")
    face_camera_index = max(
        0, _parse_int("FACE_CAMERA_INDEX", get("FACE_CAMERA_INDEX", "0"), 0)
    )

    # CAMERAS is the newer, richer form. A station that has never opened the
    # new settings page has no such key, so the two-camera list its existing
    # scalars imply is built instead — the upgrade must be a no-op.
    cameras = cameras_from_json(get("CAMERAS", ""))
    if not cameras:
        cameras = default_cameras(
            camera_mode, camera_index, camera_rtsp_url, face_camera_index
        )
    else:
        # The list is authoritative once it exists: derive the scalars from it
        # so camera_service and face_camera_service, which still read the
        # scalars, cannot disagree with what the settings page shows.
        plate = camera_for_role(cameras, ROLE_PLATE)
        if plate is not None:
            camera_mode = plate.kind
            camera_index = plate.index
            camera_rtsp_url = plate.url
        face = camera_for_role(cameras, ROLE_FACE)
        if face is not None:
            face_camera_index = face.index

    # Everything below that touches a path must see the right environment,
    # so it is activated here, in the one place every startup passes through.
    set_active_environment(environment_key(get("API_BASE_URL", "http://localhost:8000")))

    evidence_dir = get("EVIDENCE_DIR", "").strip()
    if not evidence_dir:
        # Per environment: an evidence photo taken against UAT has no business
        # in production's folder. An explicit EVIDENCE_DIR is honoured as-is.
        evidence_dir = str(get_default_evidence_dir())
    else:
        path = Path(evidence_dir)
        if not path.is_absolute():
            evidence_dir = str(get_app_data_dir() / path)

    return AppConfig(
        api_base_url=get("API_BASE_URL", "http://localhost:8000"),
        env_mode=get("ENV_MODE", "DEV"),
        auth_mode=_parse_auth_mode(get("AUTH_MODE", AUTH_MODE_MOCK)),
        portal_sso_url=get("PORTAL_SSO_URL", DEFAULT_PORTAL_SSO_URL).strip()
        or DEFAULT_PORTAL_SSO_URL,
        gate_id=get("GATE_ID", "GATE-1"),
        lane_id=get("LANE_ID", "LANE-A"),
        direction=get("DIRECTION", "ENTRY"),
        camera_mode=camera_mode,
        camera_index=camera_index,
        camera_rtsp_url=camera_rtsp_url,
        evidence_dir=evidence_dir,
        sync_interval_seconds=_parse_int(
            "SYNC_INTERVAL_SECONDS", get("SYNC_INTERVAL_SECONDS", "10"), 10
        ),
        device_name=get("DEVICE_NAME", ""),
        auto_allow_seconds=max(
            0, _parse_int("AUTO_ALLOW_SECONDS", get("AUTO_ALLOW_SECONDS", "5"), 5)
        ),
        alpr_roi=str(get("ALPR_ROI", "") or "").strip(),
        tts_voice=str(get("TTS_VOICE", "") or "").strip(),
        tts_rate=_parse_int("TTS_RATE", get("TTS_RATE", "170"), 170),
        tts_volume=_parse_float("TTS_VOLUME", get("TTS_VOLUME", "1.0"), 1.0, 0.0, 1.0),
        face_attendance_enabled=_parse_bool(
            "FACE_ATTENDANCE_ENABLED", get("FACE_ATTENDANCE_ENABLED", "true"), True
        ),
        face_camera_index=face_camera_index,
        # The face pipeline shares a CPU with a 640x640 ONNX detector + OCR
        # running at 5 fps; above ~3 fps the two starve each other and both
        # stutter. See CameraWorker.ALPR_MAX_FPS.
        face_max_fps=_parse_float(
            "FACE_MAX_FPS", get("FACE_MAX_FPS", "3"), 3.0, 0.5, 15.0
        ),
        # Defaults and floor from the department's proven live system
        # (attendance-system/face_system: tolerance 0.5, distance-only gate).
        # The 0.30 floor is not cosmetic: same-person distances run 0.2-0.45,
        # so a tolerance below it rejects every real face while the camera
        # looks perfectly healthy. A configured 0.1 did exactly that once.
        face_tolerance=_parse_float(
            "FACE_TOLERANCE", get("FACE_TOLERANCE", "0.50"), 0.50, 0.30, 0.60
        ),
        face_min_confidence=_parse_float(
            "FACE_MIN_CONFIDENCE", get("FACE_MIN_CONFIDENCE", "45.0"), 45.0, 0.0, 100.0
        ),
        cameras=cameras,
        endpoints=endpoints,
        config_path=config_path,
    )


def sync_camera_scalars(config: AppConfig) -> None:
    """Make the flat camera fields agree with the assigned camera sources.

    ``camera_service`` and ``face_camera_service`` still read the scalars, so
    they are the values that actually decide which device opens. Deriving them
    from the list — rather than asking every caller to update both — is what
    stops the settings page showing one camera while the app opens another.
    """
    plate = camera_for_role(config.cameras, ROLE_PLATE)
    if plate is not None:
        config.camera_mode = plate.kind
        config.camera_index = plate.index
        config.camera_rtsp_url = plate.url
    face = camera_for_role(config.cameras, ROLE_FACE)
    if face is not None:
        config.face_camera_index = face.index


def save_config(config: AppConfig) -> None:
    if config.cameras:
        sync_camera_scalars(config)
    lines = [
        f"API_BASE_URL={config.api_base_url}",
        f"ENV_MODE={config.env_mode}",
        f"AUTH_MODE={config.auth_mode}",
        f"PORTAL_SSO_URL={config.portal_sso_url}",
        f"GATE_ID={config.gate_id}",
        f"LANE_ID={config.lane_id}",
        f"DIRECTION={config.direction}",
        f"CAMERA_MODE={config.camera_mode}",
        f"CAMERA_INDEX={config.camera_index}",
        f"CAMERA_RTSP_URL={config.camera_rtsp_url}",
        f"EVIDENCE_DIR={config.evidence_dir}",
        f"SYNC_INTERVAL_SECONDS={config.sync_interval_seconds}",
        f"DEVICE_NAME={config.device_name}",
        f"AUTO_ALLOW_SECONDS={config.auto_allow_seconds}",
        f"ALPR_ROI={config.alpr_roi}",
        f"TTS_VOICE={config.tts_voice}",
        f"TTS_RATE={config.tts_rate}",
        f"TTS_VOLUME={config.tts_volume}",
        f"FACE_ATTENDANCE_ENABLED={str(config.face_attendance_enabled).lower()}",
        f"FACE_CAMERA_INDEX={config.face_camera_index}",
        f"FACE_MAX_FPS={config.face_max_fps}",
        f"FACE_TOLERANCE={config.face_tolerance}",
        f"FACE_MIN_CONFIDENCE={config.face_min_confidence}",
        # Written on one line, after the scalars it supersedes, so a human
        # reading the file sees the legacy keys first and this as the detail.
        f"CAMERAS={cameras_to_json(config.cameras)}",
    ]
    # Persist *every* endpoint override — previously only 8 of them were written
    # back, so AUTH_DESKTOP_*/AUTH_REFRESH/DEVICES_CHECK/EVENTS_BATCH overrides
    # were silently reset to defaults on the first Settings save.
    for key, env_var in ENDPOINT_ENV_VARS.items():
        lines.append(f"{env_var}={config.endpoints.get(key, DEFAULT_ENDPOINTS[key])}")

    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
