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

Backend order on Linux: pyttsx3 wants the *old* espeak shared library
(``libespeak.so.1``, package ``libespeak1``), which desktop Ubuntu no longer
ships — but it does ship speech-dispatcher, so when pyttsx3 cannot build, the
worker falls back to the ``spd-say`` command before giving up. On Windows
pyttsx3 drives the built-in SAPI5 voices and needs nothing extra.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
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

    def __init__(
        self,
        rate: Optional[int] = None,
        voice: str = "",
        volume: Optional[float] = None,
    ) -> None:
        self._rate = rate
        self._voice = (voice or "").strip()
        self._volume = volume
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
        if self._volume is not None:
            engine.setProperty("volume", max(0.0, min(1.0, float(self._volume))))
        if self._voice:
            # Substring match against the installed voices, case-insensitive:
            # "zira" or "female" is what a person remembers, not a registry id.
            wanted = self._voice.lower()
            for candidate in engine.getProperty("voices") or []:
                haystack = f"{candidate.id} {getattr(candidate, 'name', '')}".lower()
                if wanted in haystack:
                    engine.setProperty("voice", candidate.id)
                    break
            else:
                logger.warning(
                    "TTS_VOICE %r matches no installed voice — using the default"
                    " (run scripts/list_voices.py to see what this machine has)",
                    self._voice,
                )
        return engine

    def _find_fallback_command(self):
        """A command-line TTS to use when pyttsx3 cannot build.

        speech-dispatcher's ``spd-say`` ships with desktop Ubuntu (it powers
        the Orca screen reader), so it is present on exactly the machines where
        pyttsx3's espeak driver is most likely to be missing. ``-w`` waits for
        the utterance to finish, which is what paces the queue.
        """
        cmd = shutil.which("spd-say")
        if cmd:
            return [cmd, "-w"]
        return None

    def _run(self) -> None:
        fallback = None
        try:
            self._engine = self._build_engine()
        except Exception:
            fallback = self._find_fallback_command()
            if fallback is None:
                if not self._warned:
                    self._warned = True
                    logger.warning(
                        "No speech engine available — attendance reminders will "
                        "be shown on screen only (Linux fix: sudo apt install "
                        "libespeak1, or install speech-dispatcher)",
                        exc_info=True,
                    )
                self._available = False
                self._drain()
                return
            logger.info(
                "pyttsx3 unavailable — speaking through %s instead", fallback[0]
            )

        while True:
            text = self._queue.get()
            if text is None:
                break
            try:
                if fallback is not None:
                    subprocess.run(
                        fallback + [text],
                        timeout=30,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    self._engine.say(text)
                    self._engine.runAndWait()
            except Exception:
                # A USB headset unplugged mid-sentence must not take the
                # banner down with it.
                logger.warning("Speech playback failed — continuing silently", exc_info=True)
                self._available = False
                self._drain()
                return
        if self._engine is not None:
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


class SpdSaySpeaker:
    """Speaks through speech-dispatcher's ``spd-say`` — the Linux fallback.

    Ubuntu desktops ship ``spd-say`` (it backs the Orca screen reader) even when
    the ``libespeak.so.1`` that pyttsx3's driver wants is not installed. Same
    contract as Pyttsx3Speaker: a queue, a daemon thread, never blocks, never
    raises. ``-w`` makes each utterance finish before the next starts.
    """

    #: What TTS_VOICE accepts in this fallback (spd-say's fixed voice types).
    VOICE_TYPES = (
        "MALE1", "MALE2", "MALE3",
        "FEMALE1", "FEMALE2", "FEMALE3",
        "CHILD_MALE", "CHILD_FEMALE",
    )

    def __init__(
        self,
        rate: Optional[int] = None,
        voice: str = "",
        volume: Optional[float] = None,
    ) -> None:
        # pyttsx3 rate is words/minute (default ~200); spd-say takes -100..100.
        self._rate_arg = None
        if rate:
            self._rate_arg = str(max(-100, min(100, int((rate - 200) * 0.5))))
        self._volume_arg = None
        if volume is not None:
            self._volume_arg = str(max(-100, min(100, int(volume * 200 - 100))))
        wanted = (voice or "").strip().upper().replace(" ", "_")
        self._voice_arg = wanted if wanted in self.VOICE_TYPES else None
        if voice and self._voice_arg is None:
            logger.warning(
                "TTS_VOICE %r is not an spd-say voice type %s — using the default",
                voice, "/".join(self.VOICE_TYPES),
            )
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=_QUEUE_SIZE)
        self._thread: Optional[threading.Thread] = None
        self._available = True
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._available

    def say(self, text: str) -> None:
        if not text or not self._available:
            return
        try:
            self._ensure_thread()
            self._queue.put_nowait(text)
        except queue.Full:
            logger.debug("Speech queue full — dropping a reminder")
        except Exception:
            logger.warning("Could not queue speech — continuing silently", exc_info=True)
            self._available = False

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout=2)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="tts-spd", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        import subprocess  # noqa: PLC0415

        while True:
            text = self._queue.get()
            if text is None:
                return
            command = ["spd-say", "-w"]
            if self._rate_arg:
                command += ["-r", self._rate_arg]
            if self._volume_arg:
                command += ["-i", self._volume_arg]
            if self._voice_arg:
                command += ["-t", self._voice_arg]
            command.append(text)
            try:
                subprocess.run(
                    command, timeout=30,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                logger.warning("spd-say failed — speech disabled", exc_info=True)
                self._available = False
                return


def _pyttsx3_usable() -> bool:
    """One cheap probe so the fallback decision happens at build time.

    The probe engine is discarded: the real one must be created on the speaker
    thread (drivers want to live where they were born).
    """
    try:
        import pyttsx3  # noqa: PLC0415

        engine = pyttsx3.init()
        try:
            engine.stop()
        except Exception:
            pass
        return True
    except Exception:
        return False


def build_speaker(
    enabled: bool = True,
    voice: str = "",
    rate: Optional[int] = None,
    volume: Optional[float] = None,
) -> Speaker:
    """The speaker the app should use. Never returns ``None``, never raises.

    Preference order: pyttsx3 (SAPI on Windows, espeak on Linux when
    ``libespeak.so.1`` is installed) → ``spd-say`` (Linux desktops) → silence
    with a warning. The voice knobs come from TTS_VOICE / TTS_RATE / TTS_VOLUME.
    """
    if not enabled:
        return NullSpeaker()
    if _pyttsx3_usable():
        return Pyttsx3Speaker(rate=rate, voice=voice, volume=volume)
    import shutil  # noqa: PLC0415

    if shutil.which("spd-say"):
        logger.info("pyttsx3 unavailable — speaking through spd-say instead")
        return SpdSaySpeaker(rate=rate, voice=voice, volume=volume)
    logger.warning(
        "No speech engine available (install espeak/libespeak1 on Linux) — "
        "attendance reminders will be shown on screen only"
    )
    return NullSpeaker()
