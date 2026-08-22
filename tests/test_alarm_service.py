"""Alarm siren: it must actually initialise, and never take the UI down."""

from pathlib import Path

import pytest

from smart_gate.services.alarm_service import AlarmService
from smart_gate.utils.paths import get_alarm_sound_path


@pytest.fixture(scope="module")
def qt_app():
    """QSoundEffect needs a QCoreApplication; no event loop is started."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    pytest.importorskip("PySide6.QtMultimedia")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_alarm_wav_is_bundled():
    path = get_alarm_sound_path()
    assert path.exists(), f"alarm sound missing at {path}"
    assert path.suffix == ".wav"


def test_alarm_initialises_with_an_infinite_loop(qt_app):
    """Regression: setLoopCount(QSoundEffect.Infinite) raises TypeError in
    PySide6 — the enum member is not accepted where an int is bound. That
    silently disabled every alarm because the failure is caught and logged."""
    from PySide6.QtMultimedia import QSoundEffect

    service = AlarmService()
    assert service.available is True, "audio backend present but the effect failed to build"
    assert service._effect.loopCount() == QSoundEffect.Infinite.value


def test_start_and_stop_track_playing_state(qt_app):
    service = AlarmService()
    assert service.playing is False
    service.start()
    assert service.playing is True
    service.start()          # idempotent
    assert service.playing is True
    service.stop()
    assert service.playing is False


def test_missing_sound_file_degrades_to_silent(tmp_path: Path, qt_app):
    """No audio must never mean no alarm — the red state still has to show."""
    service = AlarmService(sound_path=tmp_path / "does-not-exist.wav")
    assert service.available is False
    service.start()          # must not raise
    assert service.playing is True
    service.stop()
    assert service.playing is False
