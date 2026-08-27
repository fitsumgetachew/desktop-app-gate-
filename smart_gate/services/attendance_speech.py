"""What the station says out loud when it recognises someone.

WORDING — edit ``PHRASES`` below and nothing else needs to change.

Deliberately *not* "authorised" / "unauthorised". That is access-control
language and it belongs to the barrier, which is the plate side of this screen.
This panel records attendance: someone whose face does not match still walks in,
they just have to record their attendance another way. Announcing "unauthorised"
over a speaker, in front of their colleagues, would be both untrue and needlessly
harsh — and it would teach the guard that the face panel decides entry, which it
does not.

Only the first name is ever spoken. The gate is a public place, a full name read
aloud carries further than it needs to, and "Thank you Fitsum" is what a person
actually responds to. Same rule as the car-without-attendance notice.

``{first_name}`` is the only placeholder; a phrase without it is fine.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from smart_gate.services.attendance_display import AttendanceStatus

# ── The words. Edit freely. ──────────────────────────────────────────
# Short on purpose: at a morning rush the station speaks once per person, and
# a two-second sentence times a hundred staff is a queue. First name only —
# never the full name (efficiency AND privacy at a public gate).
PHRASES: Dict[AttendanceStatus, str] = {
    AttendanceStatus.RECOGNISED: "Thank you, {first_name}.",
    AttendanceStatus.SUPPRESSED: "{first_name}, already recorded.",
    AttendanceStatus.UNRECOGNISED: "Not recognised. Please look at the camera.",
}

# A face the camera cannot place is re-evaluated several times a second. Without
# a floor between announcements the station would talk over itself continuously
# at anyone standing in view — which is how a helpful prompt becomes something
# staff learn to ignore, or unplug.
REPEAT_COOLDOWN_SECONDS: Dict[AttendanceStatus, float] = {
    AttendanceStatus.RECOGNISED: 5.0,
    AttendanceStatus.SUPPRESSED: 20.0,
    AttendanceStatus.UNRECOGNISED: 25.0,
}


def first_name(full_name: Optional[str]) -> str:
    """"Fitsum Tola Tola" → "Fitsum"; empty stays empty."""
    return (full_name or "").strip().split(" ")[0] if full_name else ""


class AttendanceAnnouncer:
    """Decides *whether* to speak, and what. Says nothing itself.

    Pure logic so the policy can be tested without an audio device: the caller
    hands the result to the speaker.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._last_key: Optional[str] = None
        self._last_spoken_at: float = 0.0

    def reset(self) -> None:
        self._last_key = None
        self._last_spoken_at = 0.0

    def announce(
        self,
        status: AttendanceStatus,
        full_name: Optional[str] = None,
    ) -> Optional[str]:
        """The phrase to speak for this state, or None to stay quiet.

        Silent when the state has not actually changed — the same person still
        standing there is not new information — and silent again if the same
        state returns inside its cooldown.
        """
        template = PHRASES.get(status)
        if not template:
            return None

        # Keyed by person as well as status: two different people recognised
        # back to back are two separate announcements, but one person
        # re-detected every frame is one.
        key = f"{status.value}:{full_name or ''}"
        now = self._clock()
        if key == self._last_key:
            cooldown = REPEAT_COOLDOWN_SECONDS.get(status, 10.0)
            if now - self._last_spoken_at < cooldown:
                return None

        self._last_key = key
        self._last_spoken_at = now
        return template.format(first_name=first_name(full_name)).strip()
