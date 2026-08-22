"""Signalling the barrier to open.

Today this is visual only: an indicator on the plate sidebar. The transports —
serial to an ESP32, and the ack-or-fail ``OPEN`` → ``ACK OPEN`` protocol — are
being proven separately in ``~/Software-Projects/SIT/barrier-comm-test/``, and a
later phase lifts the proven one in behind this *same* interface. That is why
``signal_open()`` is the only method the app is allowed to call: when the real
transport arrives, nothing outside this module changes.

No hardware code here, and no ``pyserial`` dependency.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class BarrierController(Protocol):
    """Signal the barrier to open.

    The signal is a convenience, never an authority: the guard keeps manual
    control at all times, and no failure of this interface may block, delay or
    hold the barrier. Callers must not wait on it and must not surface its
    errors as gate errors.
    """

    def signal_open(self) -> None: ...


class VisualBarrierController:
    """Flashes an on-screen "BARRIER OPEN SIGNAL" indicator.

    Holds a plain callable rather than a widget so the decision path can be
    tested without Qt, and so the indicator can be restyled or moved without
    touching the controller.
    """

    def __init__(self, on_signal: Optional[Callable[[], None]] = None) -> None:
        self._on_signal = on_signal
        self.signal_count = 0

    def signal_open(self) -> None:
        self.signal_count += 1
        if self._on_signal is None:
            logger.debug("Barrier open signalled with no indicator attached")
            return
        self._on_signal()


class NullBarrierController:
    """Does nothing. For configurations with no barrier signal at all."""

    def signal_open(self) -> None:
        logger.debug("Barrier open signalled (no controller configured)")


def safe_signal_open(controller: Optional[BarrierController]) -> bool:
    """Signal the barrier, swallowing anything it throws. Returns success.

    The one place the app is allowed to call a barrier from. A controller that
    raises — a widget already destroyed today, a serial port gone tomorrow — must
    never reach the decision path: by the time this is called the event row is
    already written and the gate has already decided.
    """
    if controller is None:
        return False
    try:
        controller.signal_open()
        return True
    except Exception:
        logger.warning("Barrier open signal failed — the decision stands", exc_info=True)
        return False
