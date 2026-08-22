from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# staff_uid arrives from the portal and becomes a directory name.
_SAFE_UID = re.compile(r"[^A-Za-z0-9_-]+")

APP_NAME = "SmartGate"
APP_AUTHOR = "University"


def get_app_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_AUTHOR / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_dir() -> Path:
    return ensure_dir(get_app_data_dir() / "logs")


def get_data_dir() -> Path:
    return ensure_dir(get_app_data_dir() / "data")


def get_default_db_path() -> Path:
    return get_data_dir() / "gate.db"


def get_default_evidence_dir() -> Path:
    return ensure_dir(get_app_data_dir() / "evidence")


def get_detector_model_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "models" / "detector.onnx"


def get_assets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "assets"


def get_alarm_sound_path() -> Path:
    """Looping siren played while a BLACKLISTED vehicle is on screen."""
    return get_assets_dir() / "sounds" / "alarm.wav"


def get_staff_photos_dir() -> Path:
    """Where enrolled staff photos are cached on disk.

    These are biometric data: they live under the app-data dir alongside the
    database rather than anywhere shared, and their URLs are never logged. The
    JPEG is kept (not just the embedding) so a future re-embedding — a new
    model, a changed tolerance — needs no network round trip.
    """
    return ensure_dir(get_app_data_dir() / "staff_photos")


def get_staff_photo_path(staff_uid: str, position: int) -> Path:
    """``staff_photos/<staff_uid>/<position>.jpg``.

    ``staff_uid`` comes from the portal, so it is scrubbed of anything that
    could climb out of the directory.
    """
    safe_uid = _SAFE_UID.sub("_", str(staff_uid))[:64] or "unknown"
    return get_staff_photos_dir() / safe_uid / f"{int(position)}.jpg"
