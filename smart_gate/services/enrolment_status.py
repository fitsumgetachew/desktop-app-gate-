"""How much of the staff roster this station can actually recognise.

The gate already logs "staff X has no usable face photo" during every sync, but
a log file in a guard booth is nobody's dashboard: the first anyone notices is
that the panel says "Not recognised" forever, which looks like a broken camera
rather than an empty roster.

This turns the local roster tables into something the screen can state plainly —
including, and especially, the case where the portal sent staff records with no
photos attached at all.

Pure: takes rows, returns text and a level. No Qt, no database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

# Levels the view maps to colour, matching attendance_display's vocabulary.
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_NEUTRAL = "neutral"

# Enrolment slots the portal is expected to offer per person. Five is the
# maximum, never a guarantee — a profile shot routinely yields no face.
EXPECTED_PHOTOS = 5


@dataclass(frozen=True)
class StaffEnrolment:
    """One staff member's enrolment, as this station sees it."""

    staff_uid: str
    full_name: str
    photo_count: int = 0        # photos the portal sent
    embedded_count: int = 0     # of those, how many yielded a face
    plate_count: int = 0
    last_embedded_at: Optional[int] = None
    # Appended, not inserted: this dataclass is built positionally in places,
    # and slipping a field into the middle silently shifts every argument
    # after it.
    pending_count: int = 0      # queued, not fetched yet — not a failure

    @property
    def recognisable(self) -> bool:
        return self.embedded_count > 0

    @property
    def enrolling(self) -> bool:
        return self.pending_count > 0

    @property
    def status_text(self) -> str:
        if self.photo_count == 0:
            # Not a fault. They still drive the car-without-attendance notice,
            # and a gate with no face camera never needed their face.
            return "Plates only — no face enrolment"
        if self.enrolling:
            return f"Enrolling… {self.embedded_count}/{self.photo_count}"
        if self.embedded_count == 0:
            return f"{self.photo_count} photos, none usable"
        if self.embedded_count < self.photo_count:
            return f"{self.embedded_count} of {self.photo_count} usable"
        return f"{self.embedded_count} ready"

    @property
    def level(self) -> str:
        # A warning is reserved for what someone must actually fix: photos that
        # produced no face. Having no photos at all, or still downloading them,
        # is a normal state of affairs.
        if self.photo_count == 0 or self.enrolling:
            return LEVEL_NEUTRAL
        if self.recognisable:
            return LEVEL_OK if self.embedded_count == self.photo_count else LEVEL_WARN
        return LEVEL_WARN


@dataclass(frozen=True)
class EnrolmentSummary:
    staff_total: int = 0
    photos_total: int = 0
    embedded_total: int = 0
    pending_total: int = 0
    ready_staff: int = 0
    staff_without_photos: int = 0

    @property
    def enrolling(self) -> bool:
        """Photos are still arriving — a transient state, not a fault."""
        return self.pending_total > 0

    @property
    def any_recognisable(self) -> bool:
        return self.embedded_total > 0

    @property
    def portal_sent_no_photos(self) -> bool:
        """Staff arrived, but not one photo did.

        The specific failure worth calling out by name: the roster endpoint is
        working, so nothing looks broken, yet recognition can never succeed.
        """
        return self.staff_total > 0 and self.photos_total == 0


def from_rows(rows: Iterable) -> List[StaffEnrolment]:
    """Build from ``StaffRepository.enrolment_rows()`` output."""
    result: List[StaffEnrolment] = []
    for row in rows or []:
        result.append(
            StaffEnrolment(
                staff_uid=row["staff_uid"],
                full_name=row["full_name"] or row["staff_uid"],
                photo_count=int(row["photo_count"] or 0),
                embedded_count=int(row["embedded_count"] or 0),
                pending_count=int(
                    (row["pending_count"] if "pending_count" in row.keys() else 0) or 0
                ),
                plate_count=int(row["plate_count"] or 0),
                last_embedded_at=row["last_embedded_at"],
            )
        )
    return result


def summarise(staff: Sequence[StaffEnrolment]) -> EnrolmentSummary:
    return EnrolmentSummary(
        staff_total=len(staff),
        photos_total=sum(s.photo_count for s in staff),
        embedded_total=sum(s.embedded_count for s in staff),
        pending_total=sum(s.pending_count for s in staff),
        ready_staff=sum(1 for s in staff if s.recognisable),
        staff_without_photos=sum(1 for s in staff if s.photo_count == 0),
    )


def headline(summary: EnrolmentSummary) -> tuple:
    """``(text, level)`` for the status strip — the whole state in one line.

    Deliberately says what is wrong *and where*, because every one of these
    states is fixed in the portal, not on this machine.
    """
    if summary.staff_total == 0:
        return ("No staff synced from the portal yet", LEVEL_NEUTRAL)

    if summary.enrolling:
        # Photos are fetched a few per cycle, so a first sync of a large roster
        # takes a while. Saying so beats a frozen count that looks like a hang,
        # and beats the "portal sent no photos" warning below, which would be a
        # false accusation mid-backfill.
        done = summary.photos_total - summary.pending_total
        return (
            f"Enrolling staff photos: {done}/{summary.photos_total} downloaded",
            LEVEL_NEUTRAL,
        )

    if summary.portal_sent_no_photos:
        return (
            f"{summary.staff_total} staff synced, but the portal sent no photos — "
            "face recognition cannot work until photos are enrolled there",
            LEVEL_WARN,
        )

    if not summary.any_recognisable:
        return (
            f"{summary.photos_total} photos downloaded, none usable — "
            "no face could be read from any of them",
            LEVEL_WARN,
        )

    if summary.ready_staff < summary.staff_total:
        missing = summary.staff_total - summary.ready_staff
        return (
            f"{summary.ready_staff} of {summary.staff_total} staff ready · "
            f"{summary.embedded_total} photos embedded · "
            f"{missing} cannot be recognised",
            LEVEL_WARN,
        )

    return (
        f"{summary.ready_staff} staff ready · "
        f"{summary.embedded_total} photos embedded",
        LEVEL_OK,
    )
