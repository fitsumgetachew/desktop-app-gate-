"""Local cache of the staff attendance roster.

Three tables move together and are only ever written by the roster sync:

* ``staff_roster``  — who is enrolled, plus the response-level ``version`` that
  drives the next delta (same watermark trick as ``cache_allowlist``).
* ``staff_photos``  — one row per enrolment slot, keyed by the server's content
  hash and holding the 128-d embedding.
* ``staff_plates``  — the canonical plates that belong to a staff member.

Evicting a staff member removes all three: a de-rostered person must not stay
recognisable on a gate PC. Their queued punches are *not* removed — those are
attendance records that still have to reach the portal.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from smart_gate.utils.plates import normalize_plate

# The embedding is a 128-d float64 vector; anything else in the column is a
# corrupt row from a half-written sync and is ignored rather than trusted.
ENCODING_DIM = 128
ENCODING_BYTES = ENCODING_DIM * 8


class StaffRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Sync watermark
    # ------------------------------------------------------------------

    def get_last_version(self) -> Optional[int]:
        """``None`` means "never synced" and asks the server for a full roster."""
        row = self.conn.execute("SELECT MAX(version) AS v FROM staff_roster").fetchone()
        if not row or row["v"] is None:
            return None
        return int(row["v"])

    # ------------------------------------------------------------------
    # Roster writes
    # ------------------------------------------------------------------

    def upsert_staff(
        self, staff_uid: str, full_name: str, updated_at: Optional[int], version: int
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO staff_roster (staff_uid, full_name, updated_at, version)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(staff_uid) DO UPDATE SET
                full_name=excluded.full_name,
                updated_at=excluded.updated_at,
                version=excluded.version
            """,
            (staff_uid, full_name, updated_at, version),
        )
        self.conn.commit()

    def replace_plates(self, staff_uid: str, plates: Iterable[str]) -> None:
        """Make ``plates`` the staff member's complete plate set.

        Replacing rather than upserting is what evicts a plate the portal
        dropped — an upsert-only delta would leave a sold car attributed to its
        previous owner forever.
        """
        canonical = {normalize_plate(p) for p in plates}
        canonical.discard("")
        with self.conn:
            self.conn.execute("DELETE FROM staff_plates WHERE staff_uid=?", (staff_uid,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO staff_plates (plate_number, staff_uid)"
                " VALUES (?, ?)",
                [(plate, staff_uid) for plate in sorted(canonical)],
            )

    def get_photo_hashes(self, staff_uid: str) -> Dict[int, str]:
        """``{position: photo_hash}`` for slots that are actually **settled**.

        This is the whole embedding cache: a photo whose hash is unchanged is
        never downloaded and never re-encoded, however many times its URL
        rotates.

        Only ``done`` and ``failed`` slots count. A slot still queued for
        download is deliberately absent, so the next roster sync sees it as a
        miss and re-queues it — which is what rewrites its ``source_url``. A
        signed URL can expire while it waits in the queue, and that refresh is
        the only thing that stops it being permanently unfetchable.
        """
        rows = self.conn.execute(
            "SELECT position, photo_hash FROM staff_photos"
            " WHERE staff_uid=? AND fetch_state != 'pending'",
            (staff_uid,),
        ).fetchall()
        return {int(row["position"]): row["photo_hash"] for row in rows}

    # ── Paced photo queue ─────────────────────────────────────────────

    def queue_photo(
        self, staff_uid: str, position: int, photo_hash: str, url: str
    ) -> None:
        """Mark one slot as needing a download, without fetching anything.

        Called during the roster walk so the metadata commits immediately and
        the bytes follow at whatever pace the gate can afford. Re-queuing an
        already-pending slot refreshes its URL and keeps its attempt count: the
        URL may have expired, but the failures still happened.
        """
        self.conn.execute(
            """
            INSERT INTO staff_photos
                (staff_uid, position, photo_hash, encoding, encoded_at,
                 fetch_state, download_attempts, last_error, source_url)
            VALUES (?, ?, ?, NULL, NULL, 'pending', 0, NULL, ?)
            ON CONFLICT(staff_uid, position) DO UPDATE SET
                photo_hash=excluded.photo_hash,
                source_url=excluded.source_url,
                fetch_state='pending',
                encoding=NULL,
                -- a different hash is a different photo, so its failures are
                -- not this photo's failures
                download_attempts=CASE
                    WHEN staff_photos.photo_hash = excluded.photo_hash
                    THEN staff_photos.download_attempts ELSE 0 END
            """,
            (staff_uid, int(position), photo_hash, url),
        )
        self.conn.commit()

    def pending_photos(self, limit: int, max_attempts: int) -> List[sqlite3.Row]:
        """The next slots to fetch, oldest staff first so one person completes
        before the next begins — a half-enrolled roster recognises nobody."""
        return self.conn.execute(
            """
            SELECT staff_uid, position, photo_hash, source_url, download_attempts
            FROM staff_photos
            WHERE fetch_state='pending' AND download_attempts < ?
            ORDER BY staff_uid, position
            LIMIT ?
            """,
            (int(max_attempts), int(limit)),
        ).fetchall()

    def mark_photo_failed(
        self, staff_uid: str, position: int, error: str, max_attempts: int
    ) -> None:
        """Count one failed attempt, retiring the slot once it runs out.

        A slot that keeps failing must stop being retried every cycle: it would
        occupy the budget forever and starve photos that would have worked.
        """
        self.conn.execute(
            """
            UPDATE staff_photos
            SET download_attempts = download_attempts + 1,
                last_error = ?,
                fetch_state = CASE
                    WHEN download_attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
            WHERE staff_uid=? AND position=?
            """,
            (error[:200], int(max_attempts), staff_uid, int(position)),
        )
        self.conn.commit()

    def photo_queue_progress(self) -> Tuple[int, int]:
        """``(pending, total)`` across the whole roster, for the enrolment strip."""
        row = self.conn.execute(
            "SELECT SUM(fetch_state='pending') AS pending, COUNT(*) AS total"
            " FROM staff_photos"
        ).fetchone()
        if not row:
            return 0, 0
        return int(row["pending"] or 0), int(row["total"] or 0)

    def has_pending_photos(self, staff_uid: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM staff_photos"
            " WHERE staff_uid=? AND fetch_state='pending' LIMIT 1",
            (staff_uid,),
        ).fetchone()
        return row is not None

    def count_photos(self, staff_uid: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM staff_photos WHERE staff_uid=?",
            (staff_uid,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def upsert_photo(
        self,
        staff_uid: str,
        position: int,
        photo_hash: str,
        encoding: Optional[bytes],
        encoded_at: int,
    ) -> None:
        """Store one photo slot.

        ``encoding=None`` is written deliberately: a photo that yields no face
        (a profile shot, say) still gets its row so the useless bytes are not
        re-downloaded on every cycle. Only the hash decides that.
        """
        self.conn.execute(
            """
            INSERT INTO staff_photos
                (staff_uid, position, photo_hash, encoding, encoded_at,
                 fetch_state, download_attempts, last_error)
            VALUES (?, ?, ?, ?, ?, 'done', 0, NULL)
            ON CONFLICT(staff_uid, position) DO UPDATE SET
                photo_hash=excluded.photo_hash,
                encoding=excluded.encoding,
                encoded_at=excluded.encoded_at,
                -- the single commit point: bytes fetched AND embedded. Until
                -- this runs the slot stays pending, so an interrupted backfill
                -- resumes here rather than starting over.
                fetch_state='done',
                download_attempts=0,
                last_error=NULL
            """,
            (staff_uid, int(position), photo_hash, encoding, encoded_at),
        )
        self.conn.commit()

    def delete_photos_except(self, staff_uid: str, positions: Sequence[int]) -> List[int]:
        """Drop slots the server no longer lists. Returns the positions removed."""
        keep = {int(p) for p in positions}
        current = set(self.get_photo_hashes(staff_uid))
        stale = sorted(current - keep)
        if stale:
            with self.conn:
                self.conn.executemany(
                    "DELETE FROM staff_photos WHERE staff_uid=? AND position=?",
                    [(staff_uid, position) for position in stale],
                )
        return stale

    def delete_staff(self, staff_uids: Iterable[str]) -> int:
        """Evict staff members entirely: roster row, photos and plates.

        Queued punches are left alone on purpose — they are already-recorded
        attendance and still owe the portal a delivery.
        """
        uids = [(uid,) for uid in staff_uids if uid]
        if not uids:
            return 0
        with self.conn:
            self.conn.executemany("DELETE FROM staff_photos WHERE staff_uid=?", uids)
            self.conn.executemany("DELETE FROM staff_plates WHERE staff_uid=?", uids)
            cursor = self.conn.executemany(
                "DELETE FROM staff_roster WHERE staff_uid=?", uids
            )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else len(uids)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_staff_uids(self) -> List[str]:
        rows = self.conn.execute("SELECT staff_uid FROM staff_roster").fetchall()
        return [row["staff_uid"] for row in rows]

    def get_full_name(self, staff_uid: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT full_name FROM staff_roster WHERE staff_uid=?", (staff_uid,)
        ).fetchone()
        return row["full_name"] if row else None

    def count_encodings(self, staff_uid: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM staff_photos"
            " WHERE staff_uid=? AND encoding IS NOT NULL",
            (staff_uid,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def list_encodings(self) -> List[Tuple[str, str, bytes]]:
        """``(staff_uid, full_name, encoding_blob)`` for every usable photo.

        Read once into the in-memory index; recognition must never touch SQLite
        per frame. Rows whose blob is the wrong size are skipped rather than
        crashing the load.
        """
        rows = self.conn.execute(
            """
            SELECT p.staff_uid AS staff_uid, r.full_name AS full_name,
                   p.encoding AS encoding
            FROM staff_photos p
            JOIN staff_roster r ON r.staff_uid = p.staff_uid
            WHERE p.encoding IS NOT NULL
            ORDER BY p.staff_uid, p.position
            """
        ).fetchall()
        return [
            (row["staff_uid"], row["full_name"] or row["staff_uid"], row["encoding"])
            for row in rows
            if row["encoding"] is not None and len(row["encoding"]) == ENCODING_BYTES
        ]

    def staff_for_plate(self, plate_number: str) -> List[Tuple[str, str]]:
        """``(staff_uid, full_name)`` for a plate — the join the car notice needs."""
        plate = normalize_plate(plate_number)
        if not plate:
            return []
        rows = self.conn.execute(
            """
            SELECT sp.staff_uid AS staff_uid, r.full_name AS full_name
            FROM staff_plates sp
            JOIN staff_roster r ON r.staff_uid = sp.staff_uid
            WHERE sp.plate_number = ?
            """,
            (plate,),
        ).fetchall()
        return [(row["staff_uid"], row["full_name"] or row["staff_uid"]) for row in rows]

    def enrolment_rows(self) -> List[sqlite3.Row]:
        """Per-staff enrolment counts, for the desktop's enrolment panel.

        One query with correlated sub-counts rather than N+1: this runs on the
        UI thread every few seconds, and the roster can be the whole staff list.

        ``photo_count`` is what the portal sent; ``embedded_count`` is how many
        of those actually yielded a face. The gap between them is the number
        somebody has to fix in the portal.
        """
        return self.conn.execute(
            """
            SELECT
                r.staff_uid AS staff_uid,
                r.full_name AS full_name,
                r.updated_at AS updated_at,
                (SELECT COUNT(*) FROM staff_photos p
                    WHERE p.staff_uid = r.staff_uid) AS photo_count,
                (SELECT COUNT(*) FROM staff_photos p
                    WHERE p.staff_uid = r.staff_uid
                      AND p.encoding IS NOT NULL) AS embedded_count,
                (SELECT COUNT(*) FROM staff_photos p
                    WHERE p.staff_uid = r.staff_uid
                      AND p.fetch_state = 'pending') AS pending_count,
                (SELECT COUNT(*) FROM staff_plates s
                    WHERE s.staff_uid = r.staff_uid) AS plate_count,
                (SELECT MAX(p.encoded_at) FROM staff_photos p
                    WHERE p.staff_uid = r.staff_uid
                      AND p.encoding IS NOT NULL) AS last_embedded_at
            FROM staff_roster r
            ORDER BY r.full_name COLLATE NOCASE, r.staff_uid
            """
        ).fetchall()

    def list_plates(self, staff_uid: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT plate_number FROM staff_plates WHERE staff_uid=? ORDER BY plate_number",
            (staff_uid,),
        ).fetchall()
        return [row["plate_number"] for row in rows]
