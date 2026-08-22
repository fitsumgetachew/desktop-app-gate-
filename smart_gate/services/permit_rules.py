"""Pure permit / decision policy.

No Qt, no SQLite, no HTTP — so it can be unit-tested directly and reused by
the UI, the sync worker and the online-lookup workers alike.

Two rules live here:

* **Expiry** — a cached permit whose ``valid_to`` is in the past is EXPIRED,
  even when the cached row still says ALLOWED.  Without this an expired permit
  reads as ALLOWED while the app is offline.
* **Blacklist** — a BLACKLISTED plate raises an alarm, pre-selects DENY, and
  can only be overridden with ALLOW when the guard supplies a written reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from smart_gate.utils.plates import normalize_plate
from smart_gate.utils.time import now_ts

STATUS_ALLOWED = "ALLOWED"
STATUS_DENIED = "DENIED"
STATUS_BLACKLISTED = "BLACKLISTED"
STATUS_EXPIRED = "EXPIRED"
STATUS_NOT_YET_VALID = "NOT YET VALID"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_FOUND = "NOT FOUND"

DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"


def is_blacklisted(status: Optional[str], alert: Optional[bool] = None) -> bool:
    """True when the plate is on the blacklist.

    The server also sends ``"alert": true`` on such items; it is honoured when
    present but the status string remains the authoritative fallback so the app
    keeps working against servers that do not send the flag.
    """
    if (status or "").strip().upper() == STATUS_BLACKLISTED:
        return True
    return bool(alert)


def is_expired(valid_to: Optional[int], now: Optional[int] = None) -> bool:
    """True when ``valid_to`` is set and already in the past."""
    if not valid_to:
        return False
    return int(valid_to) < (now if now is not None else now_ts())


def is_not_yet_valid(valid_from: Optional[int], now: Optional[int] = None) -> bool:
    """True when ``valid_from`` is set and still in the future.

    A permit issued for next semester must not open the barrier today.
    """
    if not valid_from:
        return False
    return int(valid_from) > (now if now is not None else now_ts())


def effective_status(
    status: Optional[str],
    valid_to: Optional[int] = None,
    now: Optional[int] = None,
    alert: Optional[bool] = None,
    valid_from: Optional[int] = None,
) -> str:
    """Resolve the status actually in force right now.

    Blacklist outranks everything (a blacklisted plate stays blacklisted after
    its permit lapses); otherwise a validity window that has elapsed downgrades
    the status to EXPIRED, and one that has not opened yet to NOT YET VALID.
    """
    raw = (status or "").strip().upper() or STATUS_UNKNOWN
    if is_blacklisted(raw, alert):
        return STATUS_BLACKLISTED
    if is_expired(valid_to, now):
        return STATUS_EXPIRED
    if is_not_yet_valid(valid_from, now):
        return STATUS_NOT_YET_VALID
    return raw


@dataclass(frozen=True)
class PlateAssessment:
    """Everything the UI needs to know about a plate, already resolved."""

    plate: str
    status: str                       # effective status (expiry/blacklist applied)
    raw_status: Optional[str]         # as cached / as returned by the server
    valid_to: Optional[int]
    found: bool
    blacklisted: bool
    expired: bool
    suggested_decision: Optional[str]  # pre-selected ALLOW / DENY, or None
    valid_from: Optional[int] = None
    not_yet_valid: bool = False

    @property
    def allowed(self) -> bool:
        return self.status == STATUS_ALLOWED

    @property
    def outside_validity_window(self) -> bool:
        """The permit exists but today is not inside its validity window."""
        return self.expired or self.not_yet_valid


def assess_plate(
    plate: str,
    status: Optional[str],
    valid_to: Optional[int] = None,
    *,
    found: bool = True,
    alert: Optional[bool] = None,
    now: Optional[int] = None,
    valid_from: Optional[int] = None,
) -> PlateAssessment:
    """Build a :class:`PlateAssessment` from a cached row or a lookup response."""
    normalized = normalize_plate(plate)
    if not found:
        return PlateAssessment(
            plate=normalized,
            status=STATUS_NOT_FOUND,
            raw_status=None,
            valid_to=None,
            found=False,
            blacklisted=False,
            expired=False,
            suggested_decision=None,
        )

    resolved = effective_status(status, valid_to, now=now, alert=alert, valid_from=valid_from)
    blacklisted = resolved == STATUS_BLACKLISTED
    expired = resolved == STATUS_EXPIRED
    not_yet_valid = resolved == STATUS_NOT_YET_VALID

    if blacklisted:
        suggested = DECISION_DENY
    elif resolved == STATUS_ALLOWED:
        suggested = DECISION_ALLOW
    elif resolved in (STATUS_DENIED, STATUS_EXPIRED, STATUS_NOT_YET_VALID):
        suggested = DECISION_DENY
    else:
        suggested = None

    return PlateAssessment(
        plate=normalized,
        status=resolved,
        raw_status=(status or "").strip().upper() or None,
        valid_to=valid_to,
        found=True,
        blacklisted=blacklisted,
        expired=expired,
        suggested_decision=suggested,
        valid_from=valid_from,
        not_yet_valid=not_yet_valid,
    )


def blacklist_override_error(
    assessment: Optional[PlateAssessment],
    decision: str,
    reason: Optional[str],
    note: Optional[str],
) -> Optional[str]:
    """Validate an ALLOW on a blacklisted plate.

    Returns an error message when the override must be refused, ``None`` when it
    may proceed.  A blacklisted plate can only be let through with a written
    justification, so both the reason and the note are mandatory.
    """
    if assessment is None or not assessment.blacklisted:
        return None
    if decision != DECISION_ALLOW:
        return None
    if not (reason or "").strip():
        return "Allowing a BLACKLISTED plate requires a manual reason."
    if not (note or "").strip():
        return (
            "Allowing a BLACKLISTED plate requires a written note explaining "
            "the override."
        )
    return None


def format_valid_to(valid_to: Optional[int], formatter) -> str:
    """Render the ' (valid to …)' / ' (expired …)' suffix for a status label."""
    if not valid_to:
        return ""
    label = "expired" if is_expired(valid_to) else "valid to"
    return f" ({label} {formatter(valid_to)})"
