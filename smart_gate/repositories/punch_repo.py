"""Offline outbox for attendance punches.

A deliberate copy of ``event_repo``: a punch is written locally the moment a
face is recognised, keeps its client-generated uuid4 as the server's idempotency
key, and is retried on every sync cycle until the portal acknowledges it or the
attempt cap is reached.

Rows are never deleted after ``synced=1``. Both the suppression window and the
daily counters read this table, so a synced punch still has work to do locally.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import List, Optional

from smart_gate.models.domain import PunchRecord

# Same cap as event_repo: after this many rejections the punch is left behind
# rather than retried forever.
MAX_SYNC_ATTEMPTS = 10

# How many punches one /attendance/batch carries.
PUNCH_BATCH_LIMIT = 200


def local_day_start(timestamp: Optional[float] = None) -> int:
    """Epoch seconds at local midnight of the day containing ``timestamp``.

    "Today" is the guard's calendar day, not UTC's: a gate in Addis
    (UTC+3) rolls over at local midnight, and a UTC boundary would reset the
    day's counters at 3 a.m. and split a night shift across two dates.
    """
    moment = datetime.fromtimestamp(time.time() if timestamp is None else timestamp)
    return int(moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class PunchRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_punch(self, punch: PunchRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO punch_queue (
                id, staff_uid, punch_time, method, confidence,
                device_id, gate_id, lane_id,
                synced, sync_attempts, last_sync_error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                punch.id,
                punch.staff_uid,
                punch.punch_time,
                punch.method,
                punch.confidence,
                punch.device_id,
                punch.gate_id,
                punch.lane_id,
                1 if punch.synced else 0,
                punch.sync_attempts,
                punch.last_sync_error,
                punch.created_at,
            ),
        )
        self.conn.commit()

    def last_punch_time(self, staff_uid: str) -> Optional[int]:
        """Newest punch for this staff member, synced or not.

        Suppression has to see *all* local punches: a punch that already
        reached the portal is exactly the one that must stop the next thirty
        frames from punching again.
        """
        row = self.conn.execute(
            "SELECT MAX(punch_time) AS t FROM punch_queue WHERE staff_uid=?",
            (staff_uid,),
        ).fetchone()
        if not row or row["t"] is None:
            return None
        return int(row["t"])

    def list_unsynced(self, limit: int = PUNCH_BATCH_LIMIT) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM punch_queue
            WHERE synced=0 AND sync_attempts < ?
            ORDER BY punch_time ASC LIMIT ?
            """,
            (MAX_SYNC_ATTEMPTS, limit),
        ).fetchall()

    def mark_synced(self, punch_id: str) -> None:
        self.conn.execute(
            "UPDATE punch_queue SET synced=1, last_sync_error=NULL WHERE id=?",
            (punch_id,),
        )
        self.conn.commit()

    def increment_sync_attempt(self, punch_id: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE punch_queue
            SET sync_attempts = COALESCE(sync_attempts, 0) + 1, last_sync_error=?
            WHERE id=?
            """,
            (error, punch_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Counters (local calendar day)
    # ------------------------------------------------------------------

    def punches_today(self, staff_uid: str, now: Optional[float] = None) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM punch_queue"
            " WHERE staff_uid=? AND punch_time >= ?",
            (staff_uid, local_day_start(now)),
        ).fetchone()
        return int(row["n"]) if row else 0

    def punch_count_today(self, now: Optional[float] = None) -> int:
        """Total punches recorded today, across all staff."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM punch_queue WHERE punch_time >= ?",
            (local_day_start(now),),
        ).fetchone()
        return int(row["n"]) if row else 0

    def staff_punched_today(self, now: Optional[float] = None) -> int:
        """Distinct staff who have punched today.

        Differs from :meth:`punch_count_today` for anyone who punched twice
        across the suppression window — "12 punches" and "9 people" are both
        legitimate readings of a day's attendance, so both are available.
        """
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT staff_uid) AS n FROM punch_queue"
            " WHERE punch_time >= ?",
            (local_day_start(now),),
        ).fetchone()
        return int(row["n"]) if row else 0

    def list_recent(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM punch_queue ORDER BY punch_time DESC LIMIT ?", (limit,)
        ).fetchall()
