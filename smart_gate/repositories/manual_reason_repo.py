from __future__ import annotations

import sqlite3
from typing import List, Optional, Tuple


class ManualReasonRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def replace_all(self, items: List[Tuple[int, str, int, int]]) -> None:
        self.conn.execute("DELETE FROM manual_reasons")
        self.conn.executemany(
            "INSERT INTO manual_reasons (id, reason_text, is_active, updated_at) VALUES (?, ?, ?, ?)",
            items,
        )
        self.conn.commit()

    def list_active(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT reason_text FROM manual_reasons WHERE is_active=1 ORDER BY id"
        ).fetchall()
        return [row["reason_text"] for row in rows]

    def get_id_by_text(self, reason_text: str) -> Optional[int]:
        """Return the integer id for a given reason text, or None if not found."""
        row = self.conn.execute(
            "SELECT id FROM manual_reasons WHERE reason_text=? AND is_active=1 LIMIT 1",
            (reason_text,),
        ).fetchone()
        return int(row["id"]) if row else None
