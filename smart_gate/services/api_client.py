from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

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

    # ------------------------------------------------------------------
    # Authentication — new desktop two-step flow
    # ------------------------------------------------------------------

    def desktop_start(self, device_id: str, email: str, password: str) -> Dict[str, Any]:
        """POST /auth/desktop/start — obtain a one-time code."""
        payload = {"device_id": device_id, "email": email, "password": password}
        resp = self.session.post(
            self._url(self.config.endpoints["AUTH_DESKTOP_START"]),
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def desktop_exchange(self, code: str, device_id: str) -> Dict[str, Any]:
        """POST /auth/desktop/exchange — exchange one-time code for tokens."""
        payload = {"code": code, "device_id": device_id}
        resp = self.session.post(
            self._url(self.config.endpoints["AUTH_DESKTOP_EXCHANGE"]),
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """POST /auth/refresh — exchange refresh token for a new access token."""
        payload = {"refresh_token": refresh_token}
        resp = self.session.post(
            self._url(self.config.endpoints["AUTH_REFRESH"]),
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def login(self, email: str, password: str) -> Dict[str, Any]:
        """POST /auth/login — direct login (admin/testing only, kept as fallback)."""
        payload = {"email": email, "password": password}
        resp = self.session.post(
            self._url(self.config.endpoints["AUTH_LOGIN"]),
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    def register_device(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(
            self._url(self.config.endpoints["DEVICES_REGISTER"]),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def check_device(self, token: str, device_id: str) -> Dict[str, Any]:
        """POST /devices/check — confirm device registration and get gate/lane assignment."""
        payload = {"device_id": device_id}
        resp = self.session.post(
            self._url(self.config.endpoints["DEVICES_CHECK"]),
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

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def get_allowlist(self, token: str, since_version: Optional[int]) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if since_version is not None:
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

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def post_event(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /events — submit a single event."""
        resp = self.session.post(
            self._url(self.config.endpoints["EVENTS"]),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def post_events_batch(self, token: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /events/batch — submit multiple events in one request."""
        resp = self.session.post(
            self._url(self.config.endpoints["EVENTS_BATCH"]),
            json={"items": items},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Evidence upload (two-step)
    # ------------------------------------------------------------------

    def get_evidence_upload_url(self, token: str, event_id: str) -> Dict[str, Any]:
        """
        POST /events/{event_id}/evidence/upload-url
        Returns: { ok, event_id, upload_method, upload_url, file_url }
        """
        resp = self.session.post(
            self._url(f"/events/{event_id}/evidence/upload-url"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def upload_evidence(
        self, upload_url: str, file_path: str, upload_method: str = "multipart"
    ) -> Dict[str, Any]:
        """
        Upload evidence file to the server.

        - upload_method="multipart"    → POST multipart/form-data (mock server and prod server path)
        - upload_method="presigned_put" → PUT raw bytes directly to cloud storage URL
        """
        # upload_url may be a relative path (/events/.../evidence) or absolute (https://...)
        if upload_url.startswith("http://") or upload_url.startswith("https://"):
            full_url = upload_url
        else:
            full_url = self._url(upload_url)

        if upload_method == "presigned_put":
            with open(file_path, "rb") as f:
                resp = self.session.put(
                    full_url,
                    data=f,
                    headers={"Content-Type": "image/jpeg"},
                    timeout=60,
                )
            resp.raise_for_status()
            return {}  # presigned PUT has no JSON response body
        else:
            # multipart/form-data, field name "file"
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "image/jpeg")}
                resp = self.session.post(full_url, files=files, timeout=60)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Vehicles
    # ------------------------------------------------------------------

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
