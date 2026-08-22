from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from smart_gate.models.domain import VehicleRecord
from smart_gate.utils.plates import normalize_plate

# (plate_number, status, valid_to, owner_name, updated_at, version[, alert])
AllowlistRow = Tuple

# Optional owner/vehicle detail columns, in the order they are written.
DETAIL_COLUMNS = (
    "owner_first_name",
    "owner_last_name",
    "relationship",
    "department",
    "phone",
    "vehicle_make",
    "vehicle_model",
    "vehicle_color",
    "valid_from",
    "note",
)

_BASE_COLUMNS = (
    "plate_number",
    "status",
    "valid_to",
    "owner_name",
    "updated_at",
    "version",
    "alert",
)

_ALL_COLUMNS = _BASE_COLUMNS + DETAIL_COLUMNS


def _as_row(item: Sequence) -> Tuple:
    """Coerce an incoming tuple to the 7-column base storage form.

    Callers may pass the historical 6-tuple (without ``alert``); the flag then
    defaults to 0 and the status string alone decides blacklist handling.
    """
    plate, status, valid_to, owner_name, updated_at, version = item[:6]
    alert = 1 if (len(item) > 6 and item[6]) else 0
    return (
        normalize_plate(plate),
        status,
        valid_to,
        owner_name,
        updated_at,
        version,
        alert,
    )


def _coerce_record(item: Any) -> Dict[str, Any]:
    """Accept either a record dict or the legacy positional tuple."""
    if isinstance(item, dict):
        return item
    plate, status, valid_to, owner_name, updated_at, version = item[:6]
    return {
        "plate_number": plate,
        "status": status,
        "valid_to": valid_to,
        "owner_name": owner_name,
        "updated_at": updated_at,
        "version": version,
        "alert": bool(len(item) > 6 and item[6]),
    }


def _as_full_row(item: Any) -> Tuple:
    """Flatten a record into the full column tuple.

    Absent keys become NULL — server responses are complete objects, so a
    missing key genuinely means "this vehicle has no value for that field".
    """
    record = _coerce_record(item)
    values = [
        normalize_plate(record.get("plate_number", "")),
        record.get("status"),
        record.get("valid_to"),
        record.get("owner_name"),
        record.get("updated_at"),
        record.get("version"),
        1 if record.get("alert") else 0,
    ]
    values.extend(record.get(column) for column in DETAIL_COLUMNS)
    return tuple(values)


def _record_from_row(row: sqlite3.Row) -> VehicleRecord:
    keys = row.keys()

    def get(name: str):
        return row[name] if name in keys else None

    return VehicleRecord(
        plate_number=row["plate_number"],
        status=row["status"],
        valid_to=get("valid_to"),
        valid_from=get("valid_from"),
        owner_name=get("owner_name"),
        owner_first_name=get("owner_first_name"),
        owner_last_name=get("owner_last_name"),
        relationship=get("relationship"),
        department=get("department"),
        phone=get("phone"),
        vehicle_make=get("vehicle_make"),
        vehicle_model=get("vehicle_model"),
        vehicle_color=get("vehicle_color"),
        note=get("note"),
        alert=bool(get("alert")),
    )


class AllowlistRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_last_version(self) -> Optional[int]:
        row = self.conn.execute("SELECT MAX(version) AS v FROM cache_allowlist").fetchone()
        if not row or row["v"] is None:
            return None
        return int(row["v"])

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_items(self, items: Iterable[Sequence]) -> None:
        """Upsert the base columns only, leaving owner/vehicle details intact.

        Each tuple: (plate_number, status, valid_to, owner_name, updated_at,
        version[, alert]).  Use :meth:`upsert_records` when the caller has the
        full detail set — this variant deliberately does not touch the detail
        columns so a base-only write can never erase them.
        """
        self.conn.executemany(
            """
            INSERT INTO cache_allowlist
                (plate_number, status, valid_to, owner_name, updated_at, version, alert)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plate_number) DO UPDATE SET
                status=excluded.status,
                valid_to=excluded.valid_to,
                owner_name=excluded.owner_name,
                updated_at=excluded.updated_at,
                version=excluded.version,
                alert=excluded.alert
            """,
            [_as_row(item) for item in items],
        )
        self.conn.commit()

    def upsert_records(self, records: Iterable[Dict[str, Any]]) -> None:
        """Upsert full vehicle records (base + owner/vehicle detail columns)."""
        rows = [_as_full_row(record) for record in records]
        if not rows:
            return
        self.conn.executemany(self._full_upsert_sql(), rows)
        self.conn.commit()

    @staticmethod
    def _full_upsert_sql() -> str:
        placeholders = ", ".join("?" for _ in _ALL_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in _ALL_COLUMNS if column != "plate_number"
        )
        return (
            f"INSERT INTO cache_allowlist ({', '.join(_ALL_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(plate_number) DO UPDATE SET {updates}"
        )

    def delete_plates(self, plates: Iterable[str]) -> int:
        """Remove revoked plates from the cache. Returns the number deleted.

        The delta sync used to upsert only, so a plate revoked on the server
        stayed ALLOWED on the gate forever.
        """
        normalized = [(normalize_plate(p),) for p in plates if normalize_plate(p)]
        if not normalized:
            return 0
        cursor = self.conn.executemany(
            "DELETE FROM cache_allowlist WHERE plate_number=?", normalized
        )
        self.conn.commit()
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else len(normalized)

    def replace_all(self, records: Iterable[Dict[str, Any]]) -> None:
        """Swap the entire cache for ``records`` — used for a full (non-delta) sync.

        Merging on a full sync would leave locally-cached plates that the server
        no longer knows about, so the whole table is rewritten in one
        transaction.
        """
        rows = [_as_full_row(record) for record in records]
        placeholders = ", ".join("?" for _ in _ALL_COLUMNS)
        with self.conn:
            self.conn.execute("DELETE FROM cache_allowlist")
            self.conn.executemany(
                f"INSERT INTO cache_allowlist ({', '.join(_ALL_COLUMNS)})"
                f" VALUES ({placeholders})",
                rows,
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_plates(self) -> List[str]:
        rows = self.conn.execute("SELECT plate_number FROM cache_allowlist").fetchall()
        return [row["plate_number"] for row in rows]

    def get_plate_status(self, plate_number: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM cache_allowlist WHERE plate_number=?",
            (normalize_plate(plate_number),),
        ).fetchone()
        return row["status"] if row else None

    def get_plate_info(self, plate_number: str) -> Optional[tuple[str, Optional[int]]]:
        row = self.conn.execute(
            "SELECT status, valid_to FROM cache_allowlist WHERE plate_number=?",
            (normalize_plate(plate_number),),
        ).fetchone()
        if not row:
            return None
        return row["status"], row["valid_to"]

    def get_plate_record(
        self, plate_number: str
    ) -> Optional[tuple[str, Optional[int], bool]]:
        """Return ``(status, valid_to, alert)`` for a cached plate, or None."""
        row = self.conn.execute(
            "SELECT status, valid_to, alert FROM cache_allowlist WHERE plate_number=?",
            (normalize_plate(plate_number),),
        ).fetchone()
        if not row:
            return None
        return row["status"], row["valid_to"], bool(row["alert"])

    def get_vehicle(self, plate_number: str) -> Optional[VehicleRecord]:
        """Return the full cached record for a plate, or None if unknown."""
        row = self.conn.execute(
            "SELECT * FROM cache_allowlist WHERE plate_number=?",
            (normalize_plate(plate_number),),
        ).fetchone()
        if not row:
            return None
        return _record_from_row(row)
