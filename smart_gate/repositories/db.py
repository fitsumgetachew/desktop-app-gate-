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
        # Server sends "alert": true alongside BLACKLISTED items
        "ALTER TABLE cache_allowlist ADD COLUMN alert INTEGER DEFAULT 0",
        # Richer vehicle/owner details shown on the decision banner. All
        # optional — an older server simply leaves them NULL.
        "ALTER TABLE cache_allowlist ADD COLUMN owner_first_name TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN owner_last_name TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN relationship TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN department TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN phone TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN vehicle_make TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN vehicle_model TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN vehicle_color TEXT",
        "ALTER TABLE cache_allowlist ADD COLUMN valid_from INTEGER",
        "ALTER TABLE cache_allowlist ADD COLUMN note TEXT",
        "ALTER TABLE event_queue ADD COLUMN manual_by_user_id TEXT",
        "ALTER TABLE event_queue ADD COLUMN manual_reason_id INTEGER",
        # Evidence upload tracking
        "ALTER TABLE event_queue ADD COLUMN evidence_upload_status TEXT",
        "ALTER TABLE event_queue ADD COLUMN evidence_uploaded_url TEXT",
        "ALTER TABLE event_queue ADD COLUMN evidence_upload_attempts INTEGER DEFAULT 0",
        # Photo fetching is paced across sync cycles, so a slot now has a state
        # of its own. It cannot be inferred from `encoding IS NULL`: that
        # already means "downloaded, but the shot had no usable face", which is
        # a finished slot, not a pending one.
        #   pending — queued, not fetched yet
        #   done    — fetched and embedded (encoding may still be NULL: no face)
        #   failed  — attempts exhausted
        # Existing rows default to 'done' because anything already stored was
        # downloaded inline by the previous build.
        "ALTER TABLE staff_photos ADD COLUMN fetch_state TEXT DEFAULT 'done'",
        "ALTER TABLE staff_photos ADD COLUMN download_attempts INTEGER DEFAULT 0",
        "ALTER TABLE staff_photos ADD COLUMN last_error TEXT",
        # Kept so a deferred fetch knows where to go. A signed absolute URL can
        # expire before the queue reaches it; every roster sync rewrites this
        # for slots that are still pending, which is what refreshes it.
        "ALTER TABLE staff_photos ADD COLUMN source_url TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists — safe to ignore

    # The access token is held in memory only (see services/token_store.py).
    # Purge any token an older build persisted here.
    try:
        conn.execute(
            "UPDATE local_device_config SET access_token=NULL WHERE access_token IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


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
            version INTEGER,
            alert INTEGER DEFAULT 0,
            owner_first_name TEXT,
            owner_last_name TEXT,
            relationship TEXT,
            department TEXT,
            phone TEXT,
            vehicle_make TEXT,
            vehicle_model TEXT,
            vehicle_color TEXT,
            valid_from INTEGER,
            note TEXT
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

    # ── Staff face attendance ────────────────────────────────────────
    # Additive only. Nothing in the gate decision path reads these tables, so a
    # station whose face stack failed to load simply leaves them empty and the
    # barrier behaves exactly as it did before this feature existed.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_roster (
            staff_uid TEXT PRIMARY KEY,
            full_name TEXT,
            updated_at INTEGER,
            version INTEGER
        )
        """
    )
    # One row per enrolled photo (positions 1-5). ``photo_hash`` is the server's
    # content hash and the ONLY cache key: signed URLs are re-issued on every
    # sync, so comparing URLs would re-download and re-embed the whole roster
    # every cycle. ``encoding`` is the 128-d float64 vector as raw bytes — it is
    # what makes recognition free at punch time.
    #
    # A NULL encoding is a legitimate stored state: some photos (profile shots)
    # yield no face at all. The row is still written so the useless photo is not
    # re-downloaded every cycle.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_photos (
            staff_uid TEXT,
            position INTEGER,
            photo_hash TEXT,
            encoding BLOB,
            encoded_at INTEGER,
            -- pending | done | failed. See _migrate_db for why this cannot be
            -- inferred from a NULL encoding.
            fetch_state TEXT DEFAULT 'done',
            download_attempts INTEGER DEFAULT 0,
            last_error TEXT,
            source_url TEXT,
            PRIMARY KEY (staff_uid, position)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_plates (
            plate_number TEXT,
            staff_uid TEXT,
            PRIMARY KEY (plate_number, staff_uid)
        )
        """
    )
    # The by-plate lookup (every ALLOW+ENTRY decision) rides the primary key's
    # leading column, so it needs no index of its own. The reverse direction —
    # a staff member's plates, walked on every eviction — does.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_staff_plates_staff ON staff_plates(staff_uid)"
    )
    # Mirrors event_queue: a client-generated uuid4 ``id`` is the idempotency
    # key, and the row survives restarts until the portal acknowledges it.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS punch_queue (
            id TEXT PRIMARY KEY,
            staff_uid TEXT,
            punch_time INTEGER,
            method TEXT,
            confidence REAL,
            device_id TEXT,
            gate_id TEXT,
            lane_id TEXT,
            synced INTEGER DEFAULT 0,
            sync_attempts INTEGER DEFAULT 0,
            last_sync_error TEXT,
            created_at INTEGER
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_punch_queue_drain"
        " ON punch_queue(synced, sync_attempts)"
    )
    # Suppression and the daily counters both filter by staff and time.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_punch_queue_staff_time"
        " ON punch_queue(staff_uid, punch_time)"
    )
    conn.commit()
    _migrate_db(conn)
