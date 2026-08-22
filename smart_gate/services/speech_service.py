"""Offline text-to-speech for the attendance reminder.

Follows ``alarm_service``'s contract: audio is an *enhancement* on top of a
visible banner, never a prerequisite. Every failure path degrades to a logged
warning and silence — a gate PC may have no audio device at all, and a spoken
reminder that cannot be produced must not cost the guard anything.

Two properties matter more than the speech itself, because ``say()`` is called
from inside the gate decision path:

* **It never blocks.** ``pyttsx3.runAndWait()`` blocks until the utterance
  finishes — seconds, on the UI thread, between the guard pressing ALLOW and the
  screen responding. So ``say()`` only enqueues; a daemon thread does the
  talking.
* **It never raises.** A missing engine, a disappeared audio device, a driver
  that throws mid-sentence: all of it stays inside this module.

pyttsx3 speaks through whatever the OS exposes, a Bluetooth speaker paired at
OS level included. That pairing is the operating system's business; this module
just calls the API.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Utterances waiting to be spoken. Small on purpose: if the engine is wedged,
# the right behaviour is to drop reminders, not to grow a backlog that replays
# ten stale names at whoever is standing there when it recovers.
_QUEUE_SIZE = 4


@runtime_checkable
class Speaker(Protocol):
    """Says a short line out loud. Implementations must never raise."""

    def say(self, text: str) -> None: ...


class NullSpeaker:
    """Says nothing, successfully.

    Used when attendance is disabled or no engine could be built, so callers
    never need to check for ``None`` before speaking.
    """

    def say(self, text: str) -> None:
        logger.debug("Speech suppressed (no speaker): %s", text)

    def stop(self) -> None:
        pass

    @property
    def available(self) -> bool:
        return False


class Pyttsx3Speaker:
    """Speaks on a private daemon thread that owns the engine.

    A pyttsx3 engine is not safe to drive from several threads, and the driver
    on Linux (espeak) wants to live on the thread that created it — so the
    engine is created inside the worker and never leaves it.
    """

    def __init__(self, rate: Optional[int] = None) -> None:
        self._rate = rate
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=_QUEUE_SIZE)
        self._thread: Optional[threading.Thread] = None
        self._engine = None
        self._available = True
        self._warned = False
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """False once the engine has proved it cannot speak."""
        return self._available

    def say(self, text: str) -> None:
        """Queue one utterance. Returns immediately; never raises."""
        if not text or not self._available:
            return
        try:
            self._ensure_thread()
            self._queue.put_nowait(text)
        except queue.Full:
            # Someone is mid-sentence and another car arrived. Dropping the
            # newer reminder is better than queueing a name to be read out
            # long after that car has gone.
            logger.debug("Speech queue full — dropping a reminder")
        except Exception:
            logger.warning("Could not queue speech — continuing silently", exc_info=True)
            self._available = False

    def stop(self) -> None:
        """Ask the worker to finish and exit. Safe to call more than once."""
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout=2)

    # ------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="tts", daemon=True
            )
            self._thread.start()

    def _build_engine(self):
        """Import and initialise pyttsx3 lazily, like the ALPR/face stacks."""
        import pyttsx3  # noqa: PLC0415 — deliberately lazy; may be absent

        engine = pyttsx3.init()
        if self._rate:
            engine.setProperty("rate", self._rate)
        return engine

    def _run(self) -> None:
        try:
            self._engine = self._build_engine()
        except Exception:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "No speech engine available — attendance reminders will be "
                    "shown on screen only",
                    exc_info=True,
                )
            self._available = False
            self._drain()
            return

        while True:
            text = self._queue.get()
            if text is None:
                break
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception:
                # A USB headset unplugged mid-sentence must not take the
                # banner down with it.
                logger.warning("Speech playback failed — continuing silently", exc_info=True)
                self._available = False
                self._drain()
                return
        try:
            self._engine.stop()
        except Exception:
            logger.debug("Speech engine stop failed", exc_info=True)

    def _drain(self) -> None:
        """Discard anything queued once speech is known to be impossible."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


def build_speaker(enabled: bool = True) -> Speaker:
    """The speaker the app should use. Never returns ``None``, never raises."""
    if not enabled:
        return NullSpeaker()
    return Pyttsx3Speaker()
