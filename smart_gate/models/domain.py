from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class UserProfile:
    uuid: str        # UUID string from server (replaces integer id)
    email: str
    full_name: str
    role: str


@dataclass
class DeviceConfig:
    device_id: str
    device_name: str
    gate_id: str
    lane_id: str
    mac_address: Optional[str]
    access_token: Optional[str]
    refresh_token: Optional[str] = None


@dataclass
class VehicleRecord:
    """A cached allowlist entry with the full owner/vehicle detail set.

    Every field beyond ``plate_number``/``status`` is optional: an older server
    that does not send the richer fields simply leaves them None, and the UI
    collapses whatever is missing rather than printing "None".
    """

    plate_number: str
    status: str
    valid_to: Optional[int] = None
    valid_from: Optional[int] = None
    owner_name: Optional[str] = None
    owner_first_name: Optional[str] = None
    owner_last_name: Optional[str] = None
    relationship: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    vehicle_color: Optional[str] = None
    note: Optional[str] = None
    alert: bool = False

    @property
    def display_owner(self) -> str:
        """Best available owner name: first+last, else the flat owner_name."""
        parts = [p for p in (self.owner_first_name, self.owner_last_name) if p and p.strip()]
        if parts:
            return " ".join(p.strip() for p in parts)
        return (self.owner_name or "").strip()

    @property
    def display_vehicle(self) -> str:
        """'Colour Make Model' with the missing pieces dropped."""
        parts = [
            p.strip()
            for p in (self.vehicle_color, self.vehicle_make, self.vehicle_model)
            if p and p.strip()
        ]
        return " ".join(parts)


@dataclass
class EventRecord:
    id: str
    event_time: int
    gate_id: str
    lane_id: str
    device_id: str
    direction: str
    plate_number_raw: str
    plate_number_final: str
    confidence: Optional[float]
    decision: str
    decision_source: str
    manual_by_user_id: Optional[str]       # UUID of guard (for API)
    manual_by_username: Optional[str]      # email/display name (fallback)
    manual_reason_id: Optional[int]        # integer id from manual_reasons table
    manual_reason: Optional[str]           # reason text (fallback)
    manual_note: Optional[str]
    is_offline_event: bool
    evidence_path: Optional[str]           # local path only, never sent to server
    synced: bool
    sync_attempts: int
    last_sync_error: Optional[str]
    created_at: int


@dataclass
class StaffMember:
    """One enrolled staff member from ``/sync/staff-roster``.

    ``plates`` are already canonical (``normalize_plate``); ``photos`` may be
    shorter than the five enrolment slots, and a photo that yields no face at
    all still appears here — it is the roster sync that decides what is usable.
    """

    staff_uid: str
    full_name: str
    updated_at: int
    plates: tuple = ()
    photos: tuple = ()


@dataclass
class StaffPhotoRef:
    """A photo slot as the server describes it.

    ``url`` is a freshly signed capability and is deliberately *not* persisted:
    it changes on every sync and says nothing about whether the bytes changed.
    Only ``photo_hash`` does.
    """

    position: int
    photo_hash: str
    url: str


@dataclass
class PunchRecord:
    """One attendance punch, queued locally until the portal acknowledges it.

    Mirrors :class:`EventRecord`: ``id`` is a client-generated uuid4 and is the
    idempotency key, so a punch replayed after a crash lands once.
    """

    id: str
    staff_uid: str
    punch_time: int
    method: str
    confidence: Optional[float]
    device_id: str
    gate_id: str
    lane_id: str
    synced: bool = False
    sync_attempts: int = 0
    last_sync_error: Optional[str] = None
    created_at: int = 0
