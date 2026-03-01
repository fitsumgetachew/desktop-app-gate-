from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, Tuple


class AllowlistRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_last_version(self) -> Optional[int]:
        row = self.conn.execute("SELECT MAX(version) AS v FROM cache_allowlist").fetchone()
        if not row or row["v"] is None:
            return None
        return int(row["v"])

    def upsert_items(self, items: Iterable[Tuple[str, str, Optional[int], int, int]]) -> None:
        self.conn.executemany(
            """
            INSERT INTO cache_allowlist (plate_number, status, valid_to, updated_at, version)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plate_number) DO UPDATE SET
                status=excluded.status,
                valid_to=excluded.valid_to,
                updated_at=excluded.updated_at,
                version=excluded.version
            """,
            list(items),
        )
        self.conn.commit()

    def get_plate_status(self, plate_number: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM cache_allowlist WHERE plate_number=?",
            (plate_number,),
        ).fetchone()
        return row["status"] if row else None

    def get_plate_info(self, plate_number: str) -> Optional[tuple[str, Optional[int]]]:
        row = self.conn.execute(
            "SELECT status, valid_to FROM cache_allowlist WHERE plate_number=?",
            (plate_number,),
        ).fetchone()
        if not row:
            return None
        return row["status"], row["valid_to"]
