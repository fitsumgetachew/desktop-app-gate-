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
