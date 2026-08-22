"""Configured camera sources and what each one is for.

The station used to have exactly two cameras hard-wired into the config: one
ALPR camera (``CAMERA_MODE``/``CAMERA_INDEX``/``CAMERA_RTSP_URL``) and one face
camera (``FACE_CAMERA_INDEX``). That works until a site has two lanes, or a
spare webcam, or an IP camera that has not been plugged in yet — at which point
the only way to change anything is to hand-edit a .env file on a gate PC.

This module makes the set of cameras a list, each with a role, so the operator
can add a source and say what it is for. The app still consumes exactly one
camera per role today; the list is what lets that grow without another config
migration.

Pure data — no OpenCV, no Qt — so the assignment rules are testable on their own.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ROLE_PLATE = "plate"
ROLE_FACE = "face"
ROLE_UNUSED = "unused"
ROLES = (ROLE_PLATE, ROLE_FACE, ROLE_UNUSED)

ROLE_LABELS = {
    ROLE_PLATE: "Plate / ALPR",
    ROLE_FACE: "Face / Attendance",
    ROLE_UNUSED: "Not used",
}

KIND_USB = "USB"
KIND_RTSP = "RTSP"
KINDS = (KIND_USB, KIND_RTSP)

# rtsp://user:password@host/... — the password must never reach a label, a log
# line or a tooltip. It stays in the config file and nowhere else.
_CREDENTIALS = re.compile(r"//[^/@\s]*:[^/@\s]*@")


def mask_url(url: str) -> str:
    """Hide the password in an RTSP URL for display.

    Hikvision URLs carry ``admin:<password>`` inline, and this string ends up on
    a screen in a guard booth and in the settings list.
    """
    if not url:
        return ""
    return _CREDENTIALS.sub("//***:***@", url)


def new_camera_id() -> str:
    return f"cam-{uuid.uuid4().hex[:8]}"


@dataclass
class CameraSource:
    """One camera the station knows about, and what it is used for."""

    id: str
    name: str
    kind: str = KIND_USB
    index: int = 0
    url: str = ""
    role: str = ROLE_UNUSED

    @property
    def is_rtsp(self) -> bool:
        return self.kind.upper() == KIND_RTSP

    @property
    def location(self) -> str:
        """Where this camera is, safe to display."""
        if self.is_rtsp:
            return mask_url(self.url) or "(no URL set)"
        return f"USB device {self.index}"

    @property
    def configured(self) -> bool:
        """False for an RTSP source with no URL — listed, but unusable."""
        return bool(self.url.strip()) if self.is_rtsp else self.index >= 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "index": self.index,
            "url": self.url,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["CameraSource"]:
        """Build from stored JSON, or ``None`` if the entry is unusable.

        Defensive throughout: this file is hand-editable, and one bad entry must
        not stop the app from starting.
        """
        if not isinstance(data, dict):
            return None
        kind = str(data.get("kind", KIND_USB)).upper()
        if kind not in KINDS:
            kind = KIND_USB
        role = str(data.get("role", ROLE_UNUSED)).lower()
        if role not in ROLES:
            role = ROLE_UNUSED
        try:
            index = int(data.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        return cls(
            id=str(data.get("id") or new_camera_id()),
            name=str(data.get("name") or "Camera"),
            kind=kind,
            index=max(0, index),
            url=str(data.get("url") or ""),
            role=role,
        )


def cameras_to_json(cameras: Sequence[CameraSource]) -> str:
    """Serialise to a single line — it has to live in a .env file."""
    return json.dumps([c.to_dict() for c in cameras], separators=(",", ":"))


def cameras_from_json(raw: str) -> List[CameraSource]:
    """Parse the stored list, tolerating anything malformed."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("CAMERAS is not valid JSON — falling back to the legacy keys")
        return []
    if not isinstance(data, list):
        logger.warning("CAMERAS is not a list — falling back to the legacy keys")
        return []
    cameras = [CameraSource.from_dict(entry) for entry in data]
    return dedupe_ids([c for c in cameras if c is not None])


def dedupe_ids(cameras: Iterable[CameraSource]) -> List[CameraSource]:
    """Give any duplicate id a fresh one — ids address rows in the UI."""
    seen: set = set()
    result: List[CameraSource] = []
    for camera in cameras:
        if camera.id in seen:
            camera.id = new_camera_id()
        seen.add(camera.id)
        result.append(camera)
    return result


def device_key(camera: "CameraSource") -> str:
    """What physical device this entry points at.

    Two entries with the same key are the same camera, however they are named.
    """
    if camera.kind.upper() == KIND_RTSP:
        return f"rtsp:{camera.url.strip().lower()}"
    return f"usb:{camera.index}"


def duplicate_device_roles(
    cameras: Sequence["CameraSource"],
) -> Optional[Tuple[str, str]]:
    """``(role_a, role_b)`` when two roles are pointed at one camera, else None.

    A USB camera can only be opened once: the second pipeline to try gets
    nothing and its preview simply stays dark. Nothing in the capture layer can
    fix that — the two jobs genuinely need two cameras — so the only useful
    place to catch it is before the settings are saved.
    """
    seen: dict = {}
    for camera in cameras:
        if camera.role == ROLE_UNUSED or not camera.configured:
            continue
        key = device_key(camera)
        if key in seen and seen[key] != camera.role:
            return seen[key], camera.role
        seen[key] = camera.role
    return None


def camera_for_role(
    cameras: Sequence[CameraSource], role: str
) -> Optional[CameraSource]:
    """The camera assigned to ``role``, or ``None``.

    First match wins: the app drives one camera per role today, and the
    assignment rules below keep that unambiguous.
    """
    for camera in cameras:
        if camera.role == role and camera.configured:
            return camera
    return None


def assign_role(
    cameras: Sequence[CameraSource], camera_id: str, role: str
) -> List[CameraSource]:
    """Give ``camera_id`` this role, taking it away from whoever had it.

    Roles are exclusive because the app can only run one ALPR pipeline and one
    face pipeline. Letting two cameras both claim "plate" would leave which one
    actually runs down to list order — a setting that silently does nothing is
    worse than one that is not offered.
    """
    if role not in ROLES:
        role = ROLE_UNUSED
    updated: List[CameraSource] = []
    for camera in cameras:
        if camera.id == camera_id:
            camera.role = role
        elif role != ROLE_UNUSED and camera.role == role:
            camera.role = ROLE_UNUSED
        updated.append(camera)
    return updated


def default_cameras(
    camera_mode: str,
    camera_index: int,
    camera_rtsp_url: str,
    face_camera_index: int,
) -> List[CameraSource]:
    """Build the two-camera list an older config implies.

    Every station upgrading from the single-camera settings lands here, so the
    result must be exactly what it was already running.
    """
    kind = KIND_RTSP if str(camera_mode).upper() == KIND_RTSP else KIND_USB
    return [
        CameraSource(
            id="cam-plate",
            name="Lane camera",
            kind=kind,
            index=max(0, int(camera_index)),
            url=camera_rtsp_url or "",
            role=ROLE_PLATE,
        ),
        CameraSource(
            id="cam-face",
            name="Attendance webcam",
            kind=KIND_USB,
            index=max(0, int(face_camera_index)),
            url="",
            role=ROLE_FACE,
        ),
    ]
