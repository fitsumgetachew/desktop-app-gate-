from __future__ import annotations

import json
import logging
import socket
import uuid
from typing import Optional

from smart_gate.models.domain import DeviceConfig
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.utils.paths import get_device_identity_path

logger = logging.getLogger(__name__)


def _format_mac(node: int) -> Optional[str]:
    if (node >> 40) % 2:
        return None
    mac = ":".join([f"{(node >> ele) & 0xff:02x}" for ele in range(40, -1, -8)])
    return mac



def load_shared_device_id() -> Optional[str]:
    """The uuid other environments on this machine already use, or None."""
    try:
        path = get_device_identity_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data.get("device_id") or "").strip().lower()
        return value or None
    except Exception:
        logger.debug("Could not read shared device identity", exc_info=True)
        return None


def remember_shared_device_id(device_id: str) -> None:
    """Best-effort: a read-only disk must not stop the gate from starting."""
    try:
        path = get_device_identity_path()
        current = load_shared_device_id()
        if current == device_id.lower():
            return
        path.write_text(
            json.dumps({"device_id": device_id.lower()}), encoding="utf-8"
        )
    except Exception:
        logger.debug("Could not persist shared device identity", exc_info=True)


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
                refresh_token=existing.refresh_token,  # preserve existing token
            )
            self.repo.upsert_device(updated)
            remember_shared_device_id(existing.device_id)
            return updated

        # Reuse the id this machine already uses against another server, if it
        # has one. Each environment provisions separately, but the operator
        # should be provisioning one consistent identity everywhere, not
        # transcribing a fresh uuid per server.
        # Lowercase everywhere: this id is transcribed into the portal by hand,
        # and one spelling is easier to match (and to eyeball) than two.
        device_id = (load_shared_device_id() or str(uuid.uuid4())).lower()
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
        remember_shared_device_id(device_id)
        logger.info("Device identity in use for this environment: %s", device_id)
        return device

    def register_device(self, token: str, device: DeviceConfig) -> None:
        payload = {
            "device_id": device.device_id,
            "device_name": device.device_name,
            "mac_address": device.mac_address,
            "gate_id": device.gate_id,
            "lane_id": device.lane_id,
            # gate_name / lane_name are optional display labels per API contract
        }
        self.api.register_device(token, payload)
