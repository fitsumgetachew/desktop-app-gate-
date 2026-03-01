from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class UserProfile:
    id: int
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
    manual_by_username: Optional[str]
    manual_reason: Optional[str]
    manual_note: Optional[str]
    is_offline_event: bool
    evidence_path: Optional[str]
    synced: bool
    sync_attempts: int
    last_sync_error: Optional[str]
    created_at: int
