"""Mapping between the server's vehicle payloads and the local cache.

``/sync/allowlist`` items, ``/vehicles/lookup`` responses and the vehicle object
returned by ``/vehicles/register-visitor`` all carry the same field set, so one
mapper serves all three.

Every field beyond ``plate_number``/``status`` is optional: an older server that
predates the richer fields simply omits them and the cache stores NULL.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from smart_gate.models.domain import VehicleRecord
from smart_gate.repositories.allowlist_repo import DETAIL_COLUMNS
from smart_gate.utils.plates import normalize_plate
from smart_gate.utils.time import now_ts


def _clean(value: Any) -> Optional[Any]:
    """Blank strings become None so the UI collapses them instead of showing ''."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def allowlist_item_to_record(item: Dict[str, Any], version: Optional[int] = None) -> Dict[str, Any]:
    """Turn one server vehicle payload into a cache record dict."""
    record: Dict[str, Any] = {
        "plate_number": normalize_plate(item.get("plate_number", "")),
        "status": item.get("status"),
        "valid_to": item.get("valid_to"),
        "owner_name": _clean(item.get("owner_name")),
        "updated_at": int(item.get("updated_at") or now_ts()),
        "version": version if version is not None else int(item.get("updated_at") or now_ts()),
        "alert": bool(item.get("alert")),
    }
    for column in DETAIL_COLUMNS:
        record[column] = _clean(item.get(column))
    return record


def record_to_vehicle(record: Dict[str, Any]) -> VehicleRecord:
    """Build the in-memory :class:`VehicleRecord` the UI renders from."""
    return VehicleRecord(
        plate_number=record.get("plate_number", ""),
        status=record.get("status") or "",
        valid_to=record.get("valid_to"),
        valid_from=record.get("valid_from"),
        owner_name=record.get("owner_name"),
        owner_first_name=record.get("owner_first_name"),
        owner_last_name=record.get("owner_last_name"),
        relationship=record.get("relationship"),
        department=record.get("department"),
        phone=record.get("phone"),
        vehicle_make=record.get("vehicle_make"),
        vehicle_model=record.get("vehicle_model"),
        vehicle_color=record.get("vehicle_color"),
        note=record.get("note"),
        alert=bool(record.get("alert")),
    )
