from __future__ import annotations

import os
import sys
from pathlib import Path

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
