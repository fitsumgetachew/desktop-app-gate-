from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

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


# ── Environment partitioning ────────────────────────────────────────────
#
# All server-specific local state lives under data/env-<key>/ and
# evidence/env-<key>/, one slot per API base URL (see utils/environment.py).
# The active key is set once by load_config(); every helper below resolves
# against it, so the rest of the app never has to thread the key through.
# Before load_config() runs, the helpers fall back to the legacy single-slot
# paths — which is exactly what an un-partitioned pre-upgrade station used.

_active_environment_key: Optional[str] = None
_LEGACY_DB_NAME = "gate.db"


def set_active_environment(key: Optional[str]) -> None:
    global _active_environment_key
    _active_environment_key = key or None


def get_active_environment() -> Optional[str]:
    return _active_environment_key


def get_env_dir(root: Path, key: Optional[str] = None) -> Path:
    key = key or _active_environment_key
    if not key:
        return ensure_dir(root)
    return ensure_dir(root / f"env-{key}")


def get_legacy_db_path() -> Path:
    """Where a pre-partitioning build kept its one database."""
    return get_data_dir() / _LEGACY_DB_NAME


def get_env_db_path(key: Optional[str] = None) -> Path:
    return get_env_dir(get_data_dir(), key) / _LEGACY_DB_NAME


def get_default_db_path() -> Path:
    """The database for the active environment (legacy path before load_config)."""
    return get_env_db_path()


def get_default_evidence_dir() -> Path:
    return get_env_dir(get_app_data_dir() / "evidence")


def adopt_legacy_database(key: str) -> Optional[Path]:
    """Move a pre-partitioning ``data/gate.db`` into ``key``'s slot, once.

    Runs on the first start after the upgrade. The legacy file holds the
    operator's provisioning, cached roster and any queued events/punches;
    stranding it would look like a wiped station. It is MOVED (with its WAL
    and shm side files), never copied: two live copies of one SQLite database
    is how you get two divergent truths.

    Only adopts when the target slot is empty — a station that already has
    data for this environment is never overwritten. Returns the new path when
    an adoption happened, else None.
    """
    legacy = get_legacy_db_path()
    if not legacy.exists():
        return None
    target = get_env_db_path(key)
    if target.exists():
        return None
    for suffix in ("", "-wal", "-shm"):
        src = legacy.with_name(legacy.name + suffix)
        if src.exists():
            src.replace(target.with_name(target.name + suffix))
    return target


def get_device_identity_path() -> Path:
    """Machine-level (not per-environment) record of this station's device_id.

    Each server provisions devices separately, so the id lives in each
    environment's database — but the operator should provision *this machine*
    under one uuid everywhere rather than transcribe a fresh one per server.
    This file is how a new environment learns the id the others already use.
    """
    return get_data_dir() / "device_identity.json"


def get_last_environment_path() -> Path:
    """Which environment the previous run used, so a switch can be announced."""
    return get_data_dir() / "last_environment.json"


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
    return get_env_dir(get_app_data_dir() / "staff_photos")


def get_staff_photo_path(staff_uid: str, position: int) -> Path:
    """``staff_photos/<staff_uid>/<position>.jpg``.

    ``staff_uid`` comes from the portal, so it is scrubbed of anything that
    could climb out of the directory.
    """
    safe_uid = _SAFE_UID.sub("_", str(staff_uid))[:64] or "unknown"
    return get_staff_photos_dir() / safe_uid / f"{int(position)}.jpg"
