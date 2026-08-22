"""What the attendance panel says.

Pure strings and levels — no Qt. The reason this is a module and not widget code
is that the wording carries the product decision: an unrecognised face is
neutral, a suppressed punch is a reassurance, and neither is ever an alarm.
"""

import pytest

from smart_gate.services.attendance_display import (
    IDLE_TEXT,
    LEVEL_IDLE,
    LEVEL_INFO,
    LEVEL_NEUTRAL,
    LEVEL_OK,
    UNRECOGNISED_TEXT,
    AttendanceStatus,
    idle,
    punch_count_text,
    recognised,
    suppressed,
    unrecognised,
)

# 2026-01-02 09:05 local, whatever the machine's zone.
import time
from datetime import datetime

MOMENT = datetime(2026, 1, 2, 9, 5).timestamp()


def test_a_recorded_punch_names_the_person_and_the_time():
    state = recognised("Abebe Bekele", MOMENT, staff_uid="stf-0001")

    assert state.status is AttendanceStatus.RECOGNISED
    assert state.text == "✓ Abebe Bekele — attendance recorded 09:05"
    assert state.level == LEVEL_OK
    assert state.staff_uid == "stf-0001"


def test_a_suppressed_punch_reads_as_reassurance_not_failure():
    """The person did nothing wrong — they already punched. A red 'rejected'
    would send them to the guard's window to complain about a system that is
    working correctly."""
    state = suppressed("Abebe Bekele", MOMENT)

    assert state.status is AttendanceStatus.SUPPRESSED
    assert state.text == "Abebe Bekele — already recorded at 09:05"
    assert state.level == LEVEL_INFO
    assert "not" not in state.text.lower()


def test_an_unrecognised_face_is_neutral_never_an_alarm():
    """Attendance is not security: this is a person the camera could not place,
    not an intruder."""
    state = unrecognised()

    assert state.status is AttendanceStatus.UNRECOGNISED
    assert state.text == UNRECOGNISED_TEXT
    assert state.level == LEVEL_NEUTRAL


def test_idle_invites_the_next_person():
    state = idle()

    assert state.status is AttendanceStatus.IDLE
    assert state.text == IDLE_TEXT
    assert state.level == LEVEL_IDLE


def test_no_state_uses_an_alarm_level():
    """There is deliberately no alarm level to reach for."""
    levels = {
        idle().level,
        recognised("A", MOMENT).level,
        suppressed("A", MOMENT).level,
        unrecognised().level,
    }
    assert "alarm" not in levels
    assert levels == {LEVEL_IDLE, LEVEL_OK, LEVEL_INFO, LEVEL_NEUTRAL}


def test_only_the_idle_state_persists():
    """Every outcome returns the panel to 'Look at the camera' on its own."""
    assert idle().transient is False
    assert recognised("A", MOMENT).transient is True
    assert suppressed("A", MOMENT).transient is True
    assert unrecognised().transient is True


def test_the_clock_is_local_time():
    """HH:MM must read as the clock on the guard's wall."""
    state = recognised("A", MOMENT)

    assert state.text.endswith(time.strftime("%H:%M", time.localtime(MOMENT)))


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "No attendance recorded yet today"),
        (1, "1 attendance record today"),
        (2, "2 attendance records today"),
        (17, "17 attendance records today"),
    ],
)
def test_the_daily_counter_reads_naturally(count, expected):
    assert punch_count_text(count) == expected
