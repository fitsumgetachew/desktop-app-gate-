from __future__ import annotations

import logging
from typing import Any, Dict

from smart_gate.models.domain import UserProfile
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.services.api_client import ApiClient

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, api: ApiClient, device_repo: DeviceRepository) -> None:
        self.api = api
        self.device_repo = device_repo

    def login(self, email: str, password: str) -> Dict[str, Any]:
        """
        Desktop two-step auth flow:
          1. POST /auth/desktop/start  → one-time code
          2. POST /auth/desktop/exchange → access_token + refresh_token + user

        Returns dict with keys: access_token, refresh_token, user
        """
        device = self.device_repo.get_device()
        if not device:
            raise RuntimeError("Device not initialised. Cannot log in.")
        device_id = device.device_id

        # Step 1 — get one-time code
        start_resp = self.api.desktop_start(device_id, email, password)
        code = start_resp["code"]

        # Step 2 — exchange code for tokens
        exchange_resp = self.api.desktop_exchange(code, device_id)
        access_token = exchange_resp["access_token"]
        refresh_token = exchange_resp["refresh_token"]
        user = exchange_resp["user"]

        # Persist tokens
        self.device_repo.update_access_token(device_id, access_token)
        self.device_repo.update_refresh_token(device_id, refresh_token)

        # Persist user profile (uuid field from new API)
        profile = UserProfile(
            uuid=user["uuid"],
            email=user["email"],
            full_name=user.get("full_name", ""),
            role=user.get("role", ""),
        )
        self.device_repo.save_user_profile(profile)

        return {"access_token": access_token, "refresh_token": refresh_token, "user": user}

    def refresh_token(self, refresh_token: str) -> str:
        """Exchange a refresh token for a new access token. Returns the new access token."""
        resp = self.api.refresh(refresh_token)
        return resp["access_token"]
