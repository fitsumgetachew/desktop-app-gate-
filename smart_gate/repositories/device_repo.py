from __future__ import annotations

import sqlite3
from typing import Optional

from smart_gate.models.domain import DeviceConfig, UserProfile
from smart_gate.utils.time import now_ts


class DeviceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_device(self) -> Optional[DeviceConfig]:
        row = self.conn.execute("SELECT * FROM local_device_config LIMIT 1").fetchone()
        if not row:
            return None
        return DeviceConfig(
            device_id=row["device_id"],
            device_name=row["device_name"],
            gate_id=row["gate_id"],
            lane_id=row["lane_id"],
            mac_address=row["mac_address"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"] if "refresh_token" in row.keys() else None,
        )

    def upsert_device(self, device: DeviceConfig) -> None:
        now = now_ts()
        self.conn.execute(
            """
            INSERT INTO local_device_config (
                device_id, device_name, gate_id, lane_id, mac_address,
                access_token, refresh_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                device_name=excluded.device_name,
                gate_id=excluded.gate_id,
                lane_id=excluded.lane_id,
                mac_address=excluded.mac_address,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                updated_at=excluded.updated_at
            """,
            (
                device.device_id,
                device.device_name,
                device.gate_id,
                device.lane_id,
                device.mac_address,
                device.access_token,
                device.refresh_token,
                now,
                now,
            ),
        )
        self.conn.commit()

    def update_access_token(self, device_id: str, token: str) -> None:
        now = now_ts()
        self.conn.execute(
            "UPDATE local_device_config SET access_token=?, updated_at=? WHERE device_id=?",
            (token, now, device_id),
        )
        self.conn.commit()

    def update_refresh_token(self, device_id: str, token: str) -> None:
        now = now_ts()
        self.conn.execute(
            "UPDATE local_device_config SET refresh_token=?, updated_at=? WHERE device_id=?",
            (token, now, device_id),
        )
        self.conn.commit()

    def save_user_profile(self, profile: UserProfile) -> None:
        now = now_ts()
        # Use rowid 1 as a fixed slot for the single logged-in user.
        # uuid is the authoritative identifier from the server.
        self.conn.execute(
            """
            INSERT INTO local_user_profile (id, uuid, email, full_name, role, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                uuid=excluded.uuid,
                email=excluded.email,
                full_name=excluded.full_name,
                role=excluded.role,
                updated_at=excluded.updated_at
            """,
            (profile.uuid, profile.email, profile.full_name, profile.role, now),
        )
        self.conn.commit()

    def get_user_profile(self) -> Optional[UserProfile]:
        row = self.conn.execute("SELECT * FROM local_user_profile LIMIT 1").fetchone()
        if not row:
            return None
        keys = row.keys()
        return UserProfile(
            uuid=row["uuid"] if "uuid" in keys else "",
            email=row["email"],
            full_name=row["full_name"],
            role=row["role"],
        )

    def clear_session(self) -> None:
        self.conn.execute(
            "UPDATE local_device_config SET access_token=NULL, refresh_token=NULL"
        )
        self.conn.execute("DELETE FROM local_user_profile")
        self.conn.commit()
