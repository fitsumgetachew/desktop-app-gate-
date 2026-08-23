"""What the attendance panel says, as a pure function of what happened.

Separated from the widget for the same reason ``decision_state`` is: the
wording and the level are the part worth testing, and testing them through a
QWidget would mean a Qt event loop for what is really a string and an enum.

The view maps ``level`` to colour. This module never imports Qt and never
mentions a colour, so a restyle cannot silently change the meaning of a state.

Note what is deliberately absent: there is no alarm level. Attendance is not
security — an unrecognised face is a person the camera did not place, not an
intruder, and it must never reach the blacklist siren.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class AttendanceStatus(Enum):
    IDLE = "IDLE"
    RECOGNISED = "RECOGNISED"
    SUPPRESSED = "SUPPRESSED"      # recognised, but inside the punch window
    UNRECOGNISED = "UNRECOGNISED"


# Level → the view's colour mapping. "ok" is the only emphatic one; a face the
# camera could not place is neutral, never a warning.
LEVEL_IDLE = "idle"
LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_NEUTRAL = "neutral"

IDLE_TEXT = "Look at the camera"

# Badge wording. Attendance language, not access control — the barrier decision
# lives on the plate side of the screen and must not be confused with this.
BADGE_RECORDED = "ATTENDANCE RECORDED"
BADGE_ALREADY = "ALREADY RECORDED"
BADGE_UNRECOGNISED = "NOT RECOGNISED"
UNRECOGNISED_TEXT = "Not recognised"

# How long a confirmation stays up before the panel returns to idle.
CONFIRMATION_HOLD_MS = 4000


@dataclass(frozen=True)
class AttendancePanelState:
    status: AttendanceStatus
    text: str
    level: str
    # Kept so the caller can log or test without re-parsing the text.
    staff_uid: Optional[str] = None
    full_name: Optional[str] = None
    # The panel is read from across a room, at a glance, by someone already
    # walking. One sentence does not survive that, so the same information is
    # also carried in three pieces the view can size independently: who
    # (headline), what happened (badge), and the detail nobody has to read.
    headline: str = ""
    badge: str = ""
    detail: str = ""

    @property
    def transient(self) -> bool:
        """True when the panel should return to idle on its own."""
        return self.status is not AttendanceStatus.IDLE


def _clock(timestamp: Optional[float] = None) -> str:
    """``HH:MM`` in the guard's local time — the clock on their wall."""
    moment = datetime.fromtimestamp(time.time() if timestamp is None else timestamp)
    return moment.strftime("%H:%M")


def idle() -> AttendancePanelState:
    return AttendancePanelState(
        AttendanceStatus.IDLE, IDLE_TEXT, LEVEL_IDLE,
        headline=IDLE_TEXT, badge="", detail="",
    )


def recognised(
    full_name: str, timestamp: Optional[float] = None, staff_uid: Optional[str] = None
) -> AttendancePanelState:
    return AttendancePanelState(
        AttendanceStatus.RECOGNISED,
        f"✓ {full_name} — attendance recorded {_clock(timestamp)}",
        LEVEL_OK,
        staff_uid=staff_uid,
        full_name=full_name,
        headline=full_name,
        badge=BADGE_RECORDED,
        detail=f"Attendance recorded at {_clock(timestamp)}",
    )


def suppressed(
    full_name: str, since: Optional[float] = None, staff_uid: Optional[str] = None
) -> AttendancePanelState:
    """The 5-minute window swallowed this punch.

    Worded as a reassurance, not a failure: the person did nothing wrong, they
    simply already punched, and a red "rejected" would send them to the guard's
    window to complain about a system working correctly.
    """
    return AttendancePanelState(
        AttendanceStatus.SUPPRESSED,
        f"{full_name} — already recorded at {_clock(since)}",
        LEVEL_INFO,
        staff_uid=staff_uid,
        full_name=full_name,
        headline=full_name,
        badge=BADGE_ALREADY,
        detail=f"Already recorded at {_clock(since)}",
    )


def unrecognised() -> AttendancePanelState:
    return AttendancePanelState(
        AttendanceStatus.UNRECOGNISED, UNRECOGNISED_TEXT, LEVEL_NEUTRAL,
        headline=UNRECOGNISED_TEXT,
        badge=BADGE_UNRECOGNISED,
        detail="Look at the camera, or see the guard",
    )


def punch_count_text(count: int) -> str:
    """The panel's running total for the day."""
    if count == 0:
        return "No attendance recorded yet today"
    if count == 1:
        return "1 attendance record today"
    return f"{count} attendance records today"
