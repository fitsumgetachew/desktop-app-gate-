from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from smart_gate.utils.config import AppConfig
from smart_gate.utils.plates import normalize_plate

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

    def logout(self, refresh_token: str, timeout: int = 5) -> None:
        """POST /auth/logout — revoke the session server-side.

        Portal-only: the reference server has no logout endpoint. Callers treat
        this as best-effort, so the short timeout matters more than the result.
        """
        resp = self.session.post(
            self._url(self.config.endpoints["AUTH_LOGOUT"]),
            json={"refresh_token": refresh_token},
            timeout=timeout,
        )
        resp.raise_for_status()

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

    def get_staff_roster(self, token: str, since_version: Optional[int]) -> Dict[str, Any]:
        """GET /sync/staff-roster — the attendance roster, same delta protocol
        as the allowlist. Each photo carries a content ``hash`` and a freshly
        signed ``url``; only the hash is stable."""
        params: Dict[str, Any] = {}
        if since_version is not None:
            params["since_version"] = since_version
        resp = self.session.get(
            self._url(self.config.endpoints["SYNC_STAFF_ROSTER"]),
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def download_photo(self, url: str, token: Optional[str] = None) -> bytes:
        """Fetch one enrolled staff photo and return its raw bytes.

        An absolute URL is a *signed* storage URL: the signature in the query
        string is the credential, so no Authorization header is sent — exactly
        as in the ``presigned_put`` branch of :meth:`upload_evidence`, where an
        extra bearer header is at best ignored by S3/GCS and at worst rejected.
        A relative URL points back at our own API and does need the token.

        The URL is never logged: it is a capability granting access to
        biometric data.
        """
        if url.startswith("http://") or url.startswith("https://"):
            # Absolute: a signed storage URL. The signature is the credential
            # and a bearer header is at best ignored, at worst rejected.
            resp = self.session.get(url, headers={}, timeout=30)
            resp.raise_for_status()
            return resp.content

        # Relative: our own API, so it takes the gate token. The server may
        # answer with the bytes (200) or hand off to storage (302).
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = self.session.get(
            self._url(url),
            headers=headers,
            timeout=30,
            # Followed by hand, deliberately. requests *would* strip the
            # Authorization header on a redirect, but only when the hostname
            # changes — a same-host hand-off (a hosting rewrite, say) keeps it
            # and would hand the gate's bearer token to the storage layer.
            # Doing the second hop ourselves makes that guarantee ours instead
            # of a side effect of someone else's redirect rules.
            allow_redirects=False,
        )
        # Tested on the status code, not resp.is_redirect: that property is
        # false when the Location header is missing, so a malformed 3xx would
        # slip past raise_for_status() (which ignores 3xx) and be returned as an
        # empty photo — silently storing zero bytes as someone's face.
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location")
            if not location:
                raise requests.HTTPError(
                    "Photo redirect carried no Location header", response=resp
                )
            # No auth on the second hop, whatever host it points at.
            followed = self.session.get(location, headers={}, timeout=30)
            followed.raise_for_status()
            return followed.content

        resp.raise_for_status()
        return resp.content

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
    # Attendance
    # ------------------------------------------------------------------

    def post_attendance_batch(
        self, token: str, items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """POST /attendance/batch — drain the punch queue.

        Same per-item contract as ``/events/batch``: each result carries ``ok``,
        the client-generated id and ``deduped``, and one bad item must not fail
        the batch.
        """
        resp = self.session.post(
            self._url(self.config.endpoints["ATTENDANCE_BATCH"]),
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
        self,
        upload_url: str,
        file_path: str,
        upload_method: str = "multipart",
        token: Optional[str] = None,
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
            # Presigned URLs carry their own auth in the query string; a bearer
            # header would be rejected by S3/GCS.
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
            # multipart/form-data, field name "file" — this hits our own API,
            # which requires the bearer token.
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "image/jpeg")}
                resp = self.session.post(full_url, files=files, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Vehicles
    # ------------------------------------------------------------------

    def lookup_vehicle(self, token: str, plate_number: str) -> Dict[str, Any]:
        endpoint = self.config.endpoints["VEHICLES_LOOKUP"].rstrip("/")
        resp = self.session.get(
            self._url(f"{endpoint}/{normalize_plate(plate_number)}"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()

    def register_visitor(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /vehicles/register-visitor — guard-accessible on-the-spot registration.

        Only ``plate_number`` is required; owner/vehicle fields are optional.
        The server caps ``valid_to`` at 30 days and answers 409 when the plate
        is blacklisted.
        """
        body = dict(payload)
        body["plate_number"] = normalize_plate(body.get("plate_number", ""))
        resp = self.session.post(
            self._url(self.config.endpoints["VEHICLES_REGISTER_VISITOR"]),
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Permits
    # ------------------------------------------------------------------

    def create_temporary_permit(self, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /permits/temporary — guard-accessible short-lived permit.

        Replaces the old guard-side call to the admin-only /vehicles/register,
        which always came back 403 for a guard session.
        Raises requests.HTTPError with a 409 response when the plate is
        blacklisted.
        """
        body = dict(payload)
        body["plate_number"] = normalize_plate(body.get("plate_number", ""))
        resp = self.session.post(
            self._url(self.config.endpoints["PERMITS_TEMPORARY"]),
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
