"""Turning a recognised face into a queued attendance punch.

Thin by design: the recognition maths lives in ``face_recognition_service`` and
the outbox mechanics in ``punch_repo``. What is here is the one policy decision
between them — how often the same person may punch.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from smart_gate.models.domain import PunchRecord
from smart_gate.repositories.punch_repo import PunchRepository
from smart_gate.services.face_recognition_service import FaceMatch
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)

# One punch per staff member per five minutes. The camera recognises the same
# face roughly three times a second, so without this a person waiting at the
# window would file hundreds of punches. Checked against *all* local punches,
# synced or not: a punch that already reached the portal is precisely the one
# that must stop the next thirty frames.
PUNCH_SUPPRESSION_SECONDS = 300

METHOD_FACE = "face"


@dataclass(frozen=True)
class PunchOutcome:
    """What happened to one recognition — the three cases prompt 5 must tell apart.

    * ``recorded``  — ``punch`` holds the row that was written.
    * suppressed    — ``suppressed_since`` holds the earlier punch's time.
    * neither       — nothing was recognised, so nothing to report.
    """

    recorded: bool
    punch: Optional[PunchRecord] = None
    suppressed_since: Optional[int] = None

    @property
    def suppressed(self) -> bool:
        return not self.recorded and self.suppressed_since is not None


class AttendanceService:
    """Writes punches for recognised staff, subject to the suppression window."""

    def __init__(
        self,
        punch_repo: PunchRepository,
        device_id: str,
        gate_id: str,
        lane_id: str,
        suppression_seconds: int = PUNCH_SUPPRESSION_SECONDS,
    ) -> None:
        self.punch_repo = punch_repo
        self.device_id = device_id
        self.gate_id = gate_id
        self.lane_id = lane_id
        self.suppression_seconds = max(0, int(suppression_seconds))

    def record_punch(
        self, match: FaceMatch, punch_time: Optional[int] = None
    ) -> PunchOutcome:
        """Queue a punch for ``match``, unless one is already inside the window."""
        moment = now_ts() if punch_time is None else int(punch_time)
        last = self.punch_repo.last_punch_time(match.staff_uid)
        if last is not None and moment - last < self.suppression_seconds:
            logger.debug(
                "Punch for %s suppressed (%ds since the last one)",
                match.staff_uid,
                moment - last,
            )
            return PunchOutcome(recorded=False, suppressed_since=last)

        punch = PunchRecord(
            id=str(uuid.uuid4()),          # the server's idempotency key
            staff_uid=match.staff_uid,
            punch_time=moment,
            method=METHOD_FACE,
            confidence=round(float(match.confidence), 2),
            device_id=self.device_id,
            gate_id=self.gate_id,
            lane_id=self.lane_id,
            synced=False,
            sync_attempts=0,
            last_sync_error=None,
            created_at=moment,
        )
        self.punch_repo.add_punch(punch)
        logger.info(
            "Attendance punch recorded for %s (%s) at %.1f%% confidence",
            match.staff_uid,
            match.full_name,
            match.confidence,
        )
        return PunchOutcome(recorded=True, punch=punch)

    # Convenience pass-throughs so callers need only this service.
    def punches_today(self, staff_uid: str) -> int:
        return self.punch_repo.punches_today(staff_uid)

    def punch_count_today(self) -> int:
        return self.punch_repo.punch_count_today()
