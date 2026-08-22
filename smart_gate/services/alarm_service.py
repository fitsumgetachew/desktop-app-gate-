"""Looping alarm siren for the BLACKLISTED state.

QtMultimedia ships with PySide6-Addons, but a gate PC may still have no audio
device (headless kiosk, no PulseAudio/PipeWire session, container). Audio is an
*enhancement* on top of the red banner — never a prerequisite — so every failure
path here degrades to a logged warning and a silent alarm.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6 import QtCore

from smart_gate.utils.paths import get_alarm_sound_path

logger = logging.getLogger(__name__)


class AlarmService(QtCore.QObject):
    """Plays ``alarm.wav`` on a loop until stopped.

    Lives entirely on the UI thread — ``QSoundEffect`` does its own buffering,
    so nothing here blocks a slot.
    """

    def __init__(self, sound_path: Optional[Path] = None, parent=None) -> None:
        super().__init__(parent)
        self._path = sound_path or get_alarm_sound_path()
        self._effect = None
        self._available = False
        self._playing = False
        self._warned = False
        self._init_effect()

    # ------------------------------------------------------------------

    def _init_effect(self) -> None:
        if not self._path or not Path(self._path).exists():
            logger.warning("Alarm sound not found at %s — alarm will be silent", self._path)
            return
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError:
            logger.warning(
                "PySide6.QtMultimedia unavailable — alarm will be silent. "
                "Install PySide6-Addons for audible alarms."
            )
            return
        try:
            effect = QSoundEffect(self)
            effect.setSource(QtCore.QUrl.fromLocalFile(str(self._path)))
            # PySide6 binds setLoopCount(int), not the Loop enum itself —
            # passing QSoundEffect.Infinite directly raises TypeError.
            effect.setLoopCount(QSoundEffect.Infinite.value)
            effect.setVolume(1.0)
        except Exception:
            logger.warning("Could not initialise the alarm sound", exc_info=True)
            return
        self._effect = effect
        self._available = True

    @property
    def available(self) -> bool:
        """True when an audible alarm can actually be produced."""
        return self._available

    @property
    def playing(self) -> bool:
        return self._playing

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin looping the siren. Safe to call repeatedly."""
        if self._playing:
            return
        self._playing = True
        if not self._available or self._effect is None:
            if not self._warned:
                logger.warning("Alarm raised but no audio backend — showing red state only")
                self._warned = True
            return
        try:
            self._effect.play()
        except Exception:
            # A device that disappears mid-session (unplugged USB headset)
            # must not take the alarm banner down with it.
            logger.warning("Alarm playback failed — showing red state only", exc_info=True)
            self._available = False

    def stop(self) -> None:
        """Silence the siren (guard acknowledged, or the state was reset)."""
        self._playing = False
        if self._effect is None:
            return
        try:
            self._effect.stop()
        except Exception:
            logger.debug("Alarm stop failed", exc_info=True)
