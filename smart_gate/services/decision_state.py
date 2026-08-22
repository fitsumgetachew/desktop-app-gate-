"""Traffic-light decision state for the camera view.

Pure logic — no Qt, no SQLite, no HTTP — so the classifier and the countdown
are unit-testable without an event loop.

Three states drive the camera section when the ALPR commits a plate:

* :data:`GateState.GREEN`  — recognized and currently valid. The only state
  that may auto-continue.
* :data:`GateState.RED`    — BLACKLISTED (alarm) or DENIED (silent). Never
  auto-anything.
* :data:`GateState.ORANGE` — unknown plate, or a permit outside its validity
  window. Offers registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from smart_gate.models.domain import VehicleRecord
from smart_gate.services.permit_rules import (
    DECISION_ALLOW,
    DECISION_DENY,
    STATUS_BLACKLISTED,
    STATUS_EXPIRED,
    STATUS_NOT_YET_VALID,
    assess_plate,
)
from smart_gate.utils.plates import normalize_plate


class GateState(str, Enum):
    IDLE = "IDLE"
    GREEN = "GREEN"
    RED = "RED"
    ORANGE = "ORANGE"


@dataclass(frozen=True)
class DecisionState:
    """What the camera view should show for the plate currently on screen."""

    state: GateState
    plate: str
    headline: str
    subtext: str = ""
    details: List[tuple] = field(default_factory=list)   # [(label, value), ...]
    alarm: bool = False              # loop the siren until acknowledged
    can_auto_allow: bool = False     # eligible for the auto-ALLOW countdown
    can_register: bool = False       # offer the "Register vehicle" action
    vehicle: Optional[VehicleRecord] = None

    @property
    def is_idle(self) -> bool:
        return self.state is GateState.IDLE


IDLE_STATE = DecisionState(state=GateState.IDLE, plate="", headline="")


def _detail_rows(vehicle: Optional[VehicleRecord], fmt_ts: Callable[[int], str]) -> List[tuple]:
    """Build the (label, value) rows for the vehicle-details panel.

    Rows with no value are omitted entirely — the panel must never show "None".
    """
    if vehicle is None:
        return []
    rows: List[tuple] = []

    def add(label: str, value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            rows.append((label, text))

    add("Owner", vehicle.display_owner)
    add("Relationship", vehicle.relationship)
    add("Department", vehicle.department)
    add("Phone", vehicle.phone)
    add("Vehicle", vehicle.display_vehicle)
    if vehicle.valid_from:
        add("Valid from", fmt_ts(vehicle.valid_from))
    if vehicle.valid_to:
        add("Valid to", fmt_ts(vehicle.valid_to))
    add("Note", vehicle.note)
    return rows


def _owner_summary(vehicle: Optional[VehicleRecord]) -> str:
    """'Owner (relationship)' for the banner, collapsing whatever is missing."""
    if vehicle is None:
        return ""
    owner = vehicle.display_owner
    relationship = (vehicle.relationship or "").strip()
    if owner and relationship:
        return f"{owner} ({relationship})"
    return owner or relationship


def classify(
    plate: str,
    vehicle: Optional[VehicleRecord],
    *,
    now: Optional[int] = None,
    fmt_ts: Callable[[int], str] = str,
) -> DecisionState:
    """Map a plate + its cached record onto a traffic-light state.

    ``vehicle=None`` means the plate is not in the cache (or the server
    answered 404) — an unknown vehicle, which is ORANGE, not RED.
    """
    normalized = normalize_plate(plate)
    if not normalized:
        return IDLE_STATE

    if vehicle is None:
        return DecisionState(
            state=GateState.ORANGE,
            plate=normalized,
            headline=f"NEW VEHICLE  —  {normalized}",
            subtext="Not registered — register the vehicle or decide manually.",
            can_register=True,
        )

    assessment = assess_plate(
        normalized,
        vehicle.status,
        vehicle.valid_to,
        alert=vehicle.alert,
        valid_from=vehicle.valid_from,
        now=now,
    )
    details = _detail_rows(vehicle, fmt_ts)
    owner = _owner_summary(vehicle)

    if assessment.blacklisted:
        return DecisionState(
            state=GateState.RED,
            plate=normalized,
            headline=f"⛔  BLACKLISTED  —  {normalized}",
            subtext=(f"{owner} — deny entry and acknowledge the alarm."
                     if owner else "Deny entry and acknowledge the alarm."),
            details=details,
            alarm=True,
            vehicle=vehicle,
        )

    if assessment.status == STATUS_EXPIRED:
        when = fmt_ts(vehicle.valid_to) if vehicle.valid_to else "unknown date"
        return DecisionState(
            state=GateState.ORANGE,
            plate=normalized,
            headline=f"PERMIT EXPIRED  —  {normalized}",
            subtext=f"Permit expired {when}." + (f"  {owner}" if owner else ""),
            details=details,
            can_register=True,
            vehicle=vehicle,
        )

    if assessment.status == STATUS_NOT_YET_VALID:
        when = fmt_ts(vehicle.valid_from) if vehicle.valid_from else "a later date"
        return DecisionState(
            state=GateState.ORANGE,
            plate=normalized,
            headline=f"PERMIT NOT YET VALID  —  {normalized}",
            subtext=f"Permit starts {when}." + (f"  {owner}" if owner else ""),
            details=details,
            can_register=True,
            vehicle=vehicle,
        )

    if assessment.allowed:
        return DecisionState(
            state=GateState.GREEN,
            plate=normalized,
            headline=f"✓  {normalized}" + (f"  —  {owner}" if owner else ""),
            subtext="Permit valid.",
            details=details,
            can_auto_allow=True,
            vehicle=vehicle,
        )

    # DENIED and anything else the server may introduce: red, but silent.
    return DecisionState(
        state=GateState.RED,
        plate=normalized,
        headline=f"{assessment.status}  —  {normalized}",
        subtext=owner or "Entry denied.",
        details=details,
        vehicle=vehicle,
    )


def suggested_decision(state: DecisionState) -> Optional[str]:
    """The button the guard most likely wants pre-selected."""
    if state.state is GateState.GREEN:
        return DECISION_ALLOW
    if state.state is GateState.RED:
        return DECISION_DENY
    return None


def is_alarm_state(status: Optional[str], alert: Optional[bool] = None) -> bool:
    """True when the siren should sound — BLACKLISTED only, not plain DENIED."""
    if (status or "").strip().upper() == STATUS_BLACKLISTED:
        return True
    return bool(alert)


class AutoAllowCountdown:
    """Countdown that auto-confirms ALLOW for a recognized vehicle.

    Kept free of Qt so the cancel-on-new-plate rules are directly testable; the
    UI just calls :meth:`tick` from a 1 s QTimer.

    The plate is part of the countdown's identity: if the ALPR commits a
    *different* plate mid-countdown the countdown must not fire for the vehicle
    that has already driven off.
    """

    def __init__(self, seconds: int = 5) -> None:
        self._default_seconds = max(0, int(seconds))
        self._plate: Optional[str] = None
        self._remaining = 0

    # ── state ────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._plate is not None

    @property
    def plate(self) -> Optional[str]:
        return self._plate

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def enabled(self) -> bool:
        """False when auto-continue is switched off (AUTO_ALLOW_SECONDS=0)."""
        return self._default_seconds > 0

    def set_seconds(self, seconds: int) -> None:
        self._default_seconds = max(0, int(seconds))

    # ── control ──────────────────────────────────────────────────────

    def start(self, plate: str, seconds: Optional[int] = None) -> bool:
        """Begin counting down for ``plate``. False when disabled or no plate."""
        normalized = normalize_plate(plate)
        total = self._default_seconds if seconds is None else max(0, int(seconds))
        if not normalized or total <= 0:
            self.cancel()
            return False
        self._plate = normalized
        self._remaining = total
        return True

    def cancel(self) -> None:
        self._plate = None
        self._remaining = 0

    def tick(self) -> bool:
        """Advance one second. Returns True on the tick that fires the ALLOW."""
        if not self.active:
            return False
        self._remaining -= 1
        if self._remaining <= 0:
            self._remaining = 0
            self._plate = None
            return True
        return False

    def on_plate_committed(self, plate: str) -> bool:
        """Handle a fresh ALPR detection while a countdown may be running.

        Returns True when the running countdown was cancelled because a
        *different* plate arrived; the caller must then re-evaluate from
        scratch. Re-detecting the same plate leaves the countdown alone so the
        timer does not restart on every frame.
        """
        normalized = normalize_plate(plate)
        if not self.active:
            return False
        if normalized == self._plate:
            return False
        self.cancel()
        return True
