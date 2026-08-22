from __future__ import annotations

import sqlite3
from typing import Optional, Tuple

from smart_gate.utils.plates import normalize_plate


class PresenceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert_presence(self, plate_number: str, last_state: str, updated_at: int) -> None:
        self.conn.execute(
            """
            INSERT INTO local_presence_hint (plate_number, last_state, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(plate_number) DO UPDATE SET
                last_state=excluded.last_state,
                updated_at=excluded.updated_at
            """,
            (normalize_plate(plate_number), last_state, updated_at),
        )
        self.conn.commit()

    def get_presence(self, plate_number: str) -> Optional[Tuple[str, int]]:
        row = self.conn.execute(
            "SELECT last_state, updated_at FROM local_presence_hint WHERE plate_number=?",
            (normalize_plate(plate_number),),
        ).fetchone()
        if not row:
            return None
        return row["last_state"], row["updated_at"]
