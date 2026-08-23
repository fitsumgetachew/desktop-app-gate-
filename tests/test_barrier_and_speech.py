"""The two interfaces bolted onto the decision path, and their containment.

Both exist to do something *after* the gate has already decided, so the property
that matters most for each is the same: nothing they do — including failing —
may reach back into the decision.
"""

import time

import pytest

from smart_gate.services.barrier_controller import (
    BarrierController,
    NullBarrierController,
    VisualBarrierController,
    safe_signal_open,
)
from smart_gate.services.speech_service import (
    NullSpeaker,
    Pyttsx3Speaker,
    Speaker,
    build_speaker,
)


# ── Barrier ───────────────────────────────────────────────────────────


def test_the_visual_controller_flashes_the_indicator():
    flashes = []
    controller = VisualBarrierController(lambda: flashes.append(1))

    controller.signal_open()

    assert flashes == [1]
    assert controller.signal_count == 1


def test_signal_open_is_the_only_method_the_app_needs():
    """A later phase swaps a serial transport in behind this interface, so the
    app must never grow a second call to depend on."""
    assert isinstance(VisualBarrierController(), BarrierController)
    assert isinstance(NullBarrierController(), BarrierController)


def test_a_controller_with_no_indicator_attached_is_harmless():
    VisualBarrierController().signal_open()          # must not raise
    NullBarrierController().signal_open()


def test_safe_signal_open_swallows_a_raising_controller():
    """A widget destroyed today, a serial port gone tomorrow — by the time this
    is called the event row is already written and the gate has decided."""

    class Boom:
        def signal_open(self):
            raise RuntimeError("port closed")

    assert safe_signal_open(Boom()) is False         # reported, not raised


def test_safe_signal_open_reports_success():
    assert safe_signal_open(VisualBarrierController(lambda: None)) is True


def test_safe_signal_open_tolerates_no_controller():
    assert safe_signal_open(None) is False


# ── Speaker ───────────────────────────────────────────────────────────


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


def test_the_fake_and_the_null_speaker_both_satisfy_the_protocol():
    assert isinstance(FakeSpeaker(), Speaker)
    assert isinstance(NullSpeaker(), Speaker)


def test_the_null_speaker_says_nothing_successfully():
    """Callers never check for None before speaking."""
    speaker = NullSpeaker()

    speaker.say("Abebe, please record your attendance.")

    assert speaker.available is False


def test_build_speaker_returns_a_null_speaker_when_attendance_is_off():
    assert isinstance(build_speaker(enabled=False), NullSpeaker)


def test_say_never_blocks_the_caller():
    """runAndWait() blocks until the utterance finishes — seconds, on the UI
    thread, between the guard pressing ALLOW and the screen responding. say()
    must only enqueue."""

    class SlowEngine:
        def say(self, text):
            time.sleep(0.4)

        def runAndWait(self):
            time.sleep(0.4)

        def stop(self):
            pass

        def setProperty(self, *a):
            pass

    speaker = Pyttsx3Speaker()
    speaker._build_engine = lambda: SlowEngine()

    started = time.perf_counter()
    speaker.say("one")
    speaker.say("two")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1          # returned long before the engine finished
    speaker.stop()


def test_a_missing_engine_degrades_to_silence_without_raising():
    """A gate PC may have no audio device at all; the banner still shows.

    Both backends are stubbed out — on a desktop Ubuntu dev box ``spd-say``
    exists, and without the second stub this test would literally speak.
    """
    speaker = Pyttsx3Speaker()

    def _no_engine():
        raise ImportError("no pyttsx3")

    speaker._build_engine = _no_engine
    speaker._find_fallback_command = lambda: None

    speaker.say("Abebe, please record your attendance.")
    speaker.stop()

    assert speaker.available is False


def test_a_missing_engine_falls_back_to_the_command_backend(monkeypatch):
    """pyttsx3 needs libespeak.so.1, which desktop Ubuntu no longer ships —
    the reminder must come out of speech-dispatcher instead of vanishing."""
    import smart_gate.services.speech_service as speech

    spoken = []

    def _fake_run(cmd, **kwargs):
        spoken.append(cmd[-1])

    monkeypatch.setattr(speech.subprocess, "run", _fake_run)

    speaker = Pyttsx3Speaker()
    speaker._build_engine = lambda: (_ for _ in ()).throw(ImportError("no pyttsx3"))
    speaker._find_fallback_command = lambda: ["/usr/bin/spd-say", "-w"]

    speaker.say("Thank you Fitsum, your attendance is recorded.")
    speaker.stop()

    assert spoken == ["Thank you Fitsum, your attendance is recorded."]
    assert speaker.available is True


def test_an_engine_that_raises_mid_sentence_is_contained():
    """A USB headset unplugged mid-utterance must not take the reminder banner
    down with it."""

    class ExplodingEngine:
        def say(self, text):
            raise RuntimeError("device gone")

        def runAndWait(self):
            pass

        def stop(self):
            pass

        def setProperty(self, *a):
            pass

    speaker = Pyttsx3Speaker()
    speaker._build_engine = lambda: ExplodingEngine()

    speaker.say("Abebe, please record your attendance.")
    for _ in range(50):
        if not speaker.available:
            break
        time.sleep(0.02)
    speaker.stop()

    assert speaker.available is False       # marked dead, nothing propagated


def test_the_queue_drops_rather_than_growing_a_backlog():
    """If the engine wedges, replaying ten stale names at whoever happens to be
    standing there when it recovers is worse than losing the reminders."""
    speaker = Pyttsx3Speaker()
    speaker._ensure_thread = lambda: None      # nothing drains the queue

    for i in range(50):
        speaker.say(f"reminder {i}")

    assert speaker._queue.qsize() <= 4
