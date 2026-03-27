from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from smart_gate.utils.paths import get_default_db_path


class Database:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or get_default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations for existing databases."""
    migrations = [
        "ALTER TABLE local_device_config ADD COLUMN refresh_token TEXT",
        "ALTER TABLE local_user_profile ADD COLUMN uuid TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN owner_name TEXT",
        "ALTER TABLE event_queue ADD COLUMN manual_by_user_id TEXT",
        "ALTER TABLE event_queue ADD COLUMN manual_reason_id INTEGER",
        # Evidence upload tracking
        "ALTER TABLE event_queue ADD COLUMN evidence_upload_status TEXT",
        "ALTER TABLE event_queue ADD COLUMN evidence_uploaded_url TEXT",
        "ALTER TABLE event_queue ADD COLUMN evidence_upload_attempts INTEGER DEFAULT 0",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists — safe to ignore


def init_db(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS local_device_config (
            device_id TEXT PRIMARY KEY,
            device_name TEXT,
            gate_id TEXT,
            lane_id TEXT,
            mac_address TEXT,
            access_token TEXT,
            created_at INTEGER,
            updated_at INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS local_user_profile (
            id INTEGER PRIMARY KEY,
            email TEXT,
            full_name TEXT,
            role TEXT,
            updated_at INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_allowlist (
            plate_number TEXT PRIMARY KEY,
            status TEXT,
            valid_to INTEGER,
            owner_name TEXT,
            updated_at INTEGER,
            version INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_reasons (
            id INTEGER PRIMARY KEY,
            reason_text TEXT,
            is_active INTEGER,
            updated_at INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_queue (
            id TEXT PRIMARY KEY,
            event_time INTEGER,
            gate_id TEXT,
            lane_id TEXT,
            device_id TEXT,
            direction TEXT,
            plate_number_raw TEXT,
            plate_number_final TEXT,
            confidence REAL,
            decision TEXT,
            decision_source TEXT,
            manual_by_user_id TEXT,
            manual_by_username TEXT,
            manual_reason_id INTEGER,
            manual_reason TEXT,
            manual_note TEXT,
            is_offline_event INTEGER,
            evidence_path TEXT,
            evidence_upload_status TEXT,
            evidence_uploaded_url TEXT,
            evidence_upload_attempts INTEGER DEFAULT 0,
            synced INTEGER,
            sync_attempts INTEGER,
            last_sync_error TEXT,
            created_at INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS local_presence_hint (
            plate_number TEXT PRIMARY KEY,
            last_state TEXT,
            updated_at INTEGER
        )
        """
    )
    conn.commit()
    _migrate_db(conn)
