from __future__ import annotations

import logging
import socket
import uuid
from typing import Optional

from smart_gate.models.domain import DeviceConfig
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.services.api_client import ApiClient

logger = logging.getLogger(__name__)


def _format_mac(node: int) -> Optional[str]:
    if (node >> 40) % 2:
        return None
    mac = ":".join([f"{(node >> ele) & 0xff:02x}" for ele in range(40, -1, -8)])
    return mac


class DeviceService:
    def __init__(self, api: ApiClient, repo: DeviceRepository) -> None:
        self.api = api
        self.repo = repo

    def ensure_device(self, gate_id: str, lane_id: str, device_name: str) -> DeviceConfig:
        existing = self.repo.get_device()
        if existing:
            updated = DeviceConfig(
                device_id=existing.device_id,
                device_name=device_name or existing.device_name,
                gate_id=gate_id,
                lane_id=lane_id,
                mac_address=existing.mac_address,
                access_token=existing.access_token,
            )
            self.repo.upsert_device(updated)
            return updated

        device_id = str(uuid.uuid4())
        node = uuid.getnode()
        mac_address = _format_mac(node)
        if not device_name:
            device_name = f"{socket.gethostname()}-{gate_id}-{lane_id}"

        device = DeviceConfig(
            device_id=device_id,
            device_name=device_name,
            gate_id=gate_id,
            lane_id=lane_id,
            mac_address=mac_address,
            access_token=None,
        )
        self.repo.upsert_device(device)
        return device

    def register_device(self, token: str, device: DeviceConfig) -> None:
        payload = {
            "device_id": device.device_id,
            "device_name": device.device_name,
            "mac_address": device.mac_address,
            "gate_id": device.gate_id,
            "lane_id": device.lane_id,
        }
        self.api.register_device(token, payload)
