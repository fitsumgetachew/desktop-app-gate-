from __future__ import annotations

import logging
from typing import Dict

from smart_gate.models.domain import UserProfile
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.services.api_client import ApiClient

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, api: ApiClient, device_repo: DeviceRepository) -> None:
        self.api = api
        self.device_repo = device_repo

    def login(self, email: str, password: str) -> Dict[str, str]:
        data = self.api.login(email, password)
        token = data["access_token"]
        user = data["user"]

        profile = UserProfile(
            id=user["id"],
            email=user["email"],
            full_name=user.get("full_name", ""),
            role=user.get("role", ""),
        )
        self.device_repo.save_user_profile(profile)

        device = self.device_repo.get_device()
        if device:
            self.device_repo.update_access_token(device.device_id, token)
        return {"access_token": token}
