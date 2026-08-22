"""The car-without-attendance join — the reason this station is worth building.

A staff car is waved through the gate; if its owner has not recorded attendance
today, they hear a short reminder while they are still at the window. That is
the whole feature, and every rule below exists to keep it from becoming a
nuisance.

Pure and injectable: the two repositories and the speaker are all passed in, so
the join is testable with no camera, no audio device and no Qt.

**This runs inside the gate decision path.** It must therefore be cheap and
total: one indexed local query, no network, no blocking, and nothing that can
raise into ``_submit_decision``. The speaking itself is somebody else's problem
— ``notice_for`` only decides *whether* to speak and *what*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from smart_gate.services.attendance_service import PUNCH_SUPPRESSION_SECONDS
from smart_gate.utils.plates import normalize_plate
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)

# Reuse the punch window: a staff car re-detected two minutes later is the same
# arrival, and nagging someone twice for one entry is worse than not nagging
# them at all.
NOTICE_SUPPRESSION_SECONDS = PUNCH_SUPPRESSION_SECONDS

DECISION_ALLOW = "ALLOW"
DIRECTION_ENTRY = "ENTRY"


@dataclass(frozen=True)
class CarNotice:
    staff_uid: str
    full_name: str
    first_name: str
    banner_text: str
    speech_text: str


def _first_name(full_name: str) -> str:
    """Only the first name is ever spoken aloud.

    The gate is a public place with a queue behind it: announcing someone's full
    name to everyone within earshot is a privacy leak the feature does not need.
    """
    parts = (full_name or "").strip().split()
    return parts[0] if parts else "there"


class CarNoticeService:
    """Decides whether an entering car earns its owner a spoken reminder."""

    def __init__(
        self,
        staff_repo,
        punch_repo,
        suppression_seconds: int = NOTICE_SUPPRESSION_SECONDS,
    ) -> None:
        self.staff_repo = staff_repo
        self.punch_repo = punch_repo
        self.suppression_seconds = max(0, int(suppression_seconds))
        # staff_uid → when they were last reminded. In memory only: a restart
        # re-arming the reminder is harmless, persisting it is not worth a table.
        self._notified: Dict[str, int] = {}

    def notice_for(
        self,
        plate_number: str,
        decision: str,
        direction: str,
        now: Optional[int] = None,
    ) -> Optional[CarNotice]:
        """The notice this decision earns, or ``None`` for silence.

        Silent when: the decision was not ALLOW, the direction was not ENTRY,
        the plate belongs to nobody on the roster, the owner has already punched
        today, or they were reminded within the suppression window.
        """
        if decision != DECISION_ALLOW or direction != DIRECTION_ENTRY:
            return None

        plate = normalize_plate(plate_number)
        if not plate:
            return None

        moment = now_ts() if now is None else int(now)
        owners = self.staff_repo.staff_for_plate(plate)
        if not owners:
            return None

        for staff_uid, full_name in owners:
            # A car can be shared. Remind whoever is actually missing a punch
            # rather than giving up because one of the owners is already in.
            if self.punch_repo.punches_today(staff_uid) > 0:
                continue
            last = self._notified.get(staff_uid)
            if last is not None and moment - last < self.suppression_seconds:
                continue
            self._notified[staff_uid] = moment
            first = _first_name(full_name)
            # staff_uid only — a plate or a full name at INFO level would put
            # both in the gate's log file for anyone who reads it.
            logger.info("Attendance reminder for staff %s", staff_uid)
            return CarNotice(
                staff_uid=staff_uid,
                full_name=full_name,
                first_name=first,
                banner_text=f"{first} has not recorded attendance today",
                speech_text=f"{first}, please record your attendance.",
            )
        return None

    def forget(self, staff_uid: str) -> None:
        """Re-arm the reminder for one person (they punched, or a new day)."""
        self._notified.pop(staff_uid, None)

    def reset(self) -> None:
        self._notified.clear()
