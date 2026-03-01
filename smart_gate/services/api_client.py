from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from smart_gate.utils.config import AppConfig

logger = logging.getLogger(__name__)


class ApiClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def _url(self, endpoint: str) -> str:
        base = self.config.api_base_url.rstrip("/")
        return f"{base}{endpoint}"

    def login(self, email: str, password: str) -> Dict[str, Any]:
        payload = {"email": email, "password": password}
        resp = self.session.post(
            self._url(self.config.endpoints["AUTH_LOGIN"]),
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def register_device(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(
            self._url(self.config.endpoints["DEVICES_REGISTER"]),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def heartbeat(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(
            self._url(self.config.endpoints["DEVICES_HEARTBEAT"]),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()

    def get_allowlist(self, token: str, since_version: Optional[int]) -> Dict[str, Any]:
        params = {}
        if since_version:
            params["since_version"] = since_version
        resp = self.session.get(
            self._url(self.config.endpoints["SYNC_ALLOWLIST"]),
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_manual_reasons(self, token: str) -> Dict[str, Any]:
        resp = self.session.get(
            self._url(self.config.endpoints["SYNC_MANUAL_REASONS"]),
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def post_event(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(
            self._url(self.config.endpoints["EVENTS"]),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def lookup_vehicle(self, token: str, plate_number: str) -> Dict[str, Any]:
        endpoint = self.config.endpoints["VEHICLES_LOOKUP"].rstrip("/")
        resp = self.session.get(
            self._url(f"{endpoint}/{plate_number}"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def register_vehicle(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(
            self._url(self.config.endpoints["VEHICLES_REGISTER"]),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
