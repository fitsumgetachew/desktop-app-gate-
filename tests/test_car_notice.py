"""The car-without-attendance join.

Pure: fake repositories, no camera, no audio, no Qt. The rules being pinned are
the ones that decide whether a person standing at a gate gets spoken to, so each
silence has its own test — a notice that fires when it shouldn't is worse than
one that doesn't fire at all.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from smart_gate.repositories.db import init_db
from smart_gate.repositories.punch_repo import PunchRepository, local_day_start
from smart_gate.services.attendance_service import AttendanceService
from smart_gate.services.car_notice import (
    NOTICE_SUPPRESSION_SECONDS,
    CarNoticeService,
    _first_name,
)
from smart_gate.services.face_recognition_service import FaceMatch

STAFF_PLATE = "AA12345"
VISITOR_PLATE = "ZZ00000"
ABEBE = ("stf-0001", "Abebe Bekele")


class FakeStaffRepo:
    def __init__(self, mapping=None):
        self.mapping = mapping or {STAFF_PLATE: [ABEBE]}
        self.lookups = []

    def staff_for_plate(self, plate):
        self.lookups.append(plate)
        return self.mapping.get(plate, [])


class FakePunchRepo:
    def __init__(self, counts=None):
        self.counts = counts or {}

    def punches_today(self, staff_uid, now=None):
        return self.counts.get(staff_uid, 0)


@pytest.fixture
def service():
    return CarNoticeService(FakeStaffRepo(), FakePunchRepo())


# ── It fires ──────────────────────────────────────────────────────────


def test_a_staff_car_entering_without_a_punch_earns_a_notice(service):
    notice = service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000)

    assert notice is not None
    assert notice.staff_uid == "stf-0001"
    assert notice.banner_text == "Abebe has not recorded attendance today"
    assert notice.speech_text == "Abebe, please record your attendance."


def test_the_plate_is_canonicalised_before_the_lookup(service):
    """The guard may type 'AA-123 45'; the table only holds canonical plates."""
    assert service.notice_for("aa-123 45", "ALLOW", "ENTRY", now=1_000_000) is not None
    assert service.staff_repo.lookups == ["AA12345"]


def test_only_the_first_name_is_ever_spoken(service):
    """The gate has a queue behind it — announcing a full name tells everyone
    within earshot who just arrived."""
    notice = service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000)

    assert "Bekele" not in notice.speech_text
    assert "Bekele" not in notice.banner_text
    assert notice.full_name == "Abebe Bekele"      # still available to the caller


@pytest.mark.parametrize(
    "full_name,expected",
    [("Abebe Bekele", "Abebe"), ("Sara", "Sara"), ("  Hanna  Girma ", "Hanna"),
     ("", "there"), (None, "there")],
)
def test_first_name_extraction(full_name, expected):
    assert _first_name(full_name) == expected


# ── It stays silent ───────────────────────────────────────────────────


def test_silent_when_the_staff_member_already_punched_today():
    service = CarNoticeService(FakeStaffRepo(), FakePunchRepo({"stf-0001": 1}))

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000) is None


def test_silent_for_a_plate_that_belongs_to_nobody(service):
    assert service.notice_for(VISITOR_PLATE, "ALLOW", "ENTRY", now=1_000_000) is None


def test_silent_on_deny(service):
    """A car that was refused entry is not a car whose owner should be nagged."""
    assert service.notice_for(STAFF_PLATE, "DENY", "ENTRY", now=1_000_000) is None


def test_silent_on_exit(service):
    """Leaving without a punch is not something a reminder can still fix."""
    assert service.notice_for(STAFF_PLATE, "ALLOW", "EXIT", now=1_000_000) is None


def test_silent_for_an_empty_plate(service):
    assert service.notice_for("", "ALLOW", "ENTRY", now=1_000_000) is None
    assert service.staff_repo.lookups == []          # not even a query


# ── Once per window ───────────────────────────────────────────────────


def test_the_window_matches_the_punch_window():
    assert NOTICE_SUPPRESSION_SECONDS == 300


def test_a_re_detection_inside_the_window_is_silent(service):
    """The same car rolling forward two minutes later is one arrival."""
    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000) is not None

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_120) is None
    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_299) is None


def test_a_return_after_the_window_earns_a_new_notice(service):
    service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000)

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_300) is not None


def test_suppression_is_per_staff_member():
    staff_repo = FakeStaffRepo(
        {STAFF_PLATE: [ABEBE], "BB99887": [("stf-0002", "Sara Tesfaye")]}
    )
    service = CarNoticeService(staff_repo, FakePunchRepo())
    service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000)

    assert service.notice_for("BB99887", "ALLOW", "ENTRY", now=1_000_001) is not None


def test_punching_re_arms_the_reminder(service):
    """They were reminded, they walked in and punched — a later entry that day
    is silent because of the punch, not because of a stale suppression flag."""
    service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000)
    service.forget("stf-0001")

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_010) is not None


# ── Shared cars ───────────────────────────────────────────────────────


def test_a_shared_car_reminds_whoever_is_missing_a_punch():
    """One plate, two owners: the one who has not punched is the one reminded."""
    staff_repo = FakeStaffRepo({STAFF_PLATE: [ABEBE, ("stf-0002", "Sara Tesfaye")]})
    service = CarNoticeService(staff_repo, FakePunchRepo({"stf-0001": 1}))

    notice = service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000)

    assert notice.staff_uid == "stf-0002"
    assert notice.first_name == "Sara"


def test_a_shared_car_is_silent_when_every_owner_has_punched():
    staff_repo = FakeStaffRepo({STAFF_PLATE: [ABEBE, ("stf-0002", "Sara Tesfaye")]})
    service = CarNoticeService(
        staff_repo, FakePunchRepo({"stf-0001": 1, "stf-0002": 2})
    )

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY", now=1_000_000) is None


# ── "Today" is the local calendar day ─────────────────────────────────


@pytest.fixture
def real_punch_repo(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield PunchRepository(conn)
    conn.close()


def test_a_punch_from_yesterday_evening_does_not_count_as_today(real_punch_repo):
    """A gate in UTC+3 rolls over at local midnight. Under a UTC boundary, an
    11 p.m. punch would still count as 'today' at 2 a.m. and the reminder would
    wrongly stay silent."""
    yesterday_evening = local_day_start() - 3600      # 23:00 local, yesterday
    AttendanceService(
        real_punch_repo, "dev-1", "GATE-1", "LANE-A"
    ).record_punch(
        FaceMatch("stf-0001", "Abebe Bekele", 75.5, 0.245),
        punch_time=yesterday_evening,
    )
    assert real_punch_repo.punches_today("stf-0001") == 0

    service = CarNoticeService(FakeStaffRepo(), real_punch_repo)

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY") is not None


def test_a_punch_this_morning_does_count(real_punch_repo):
    AttendanceService(
        real_punch_repo, "dev-1", "GATE-1", "LANE-A"
    ).record_punch(
        FaceMatch("stf-0001", "Abebe Bekele", 75.5, 0.245),
        punch_time=local_day_start() + 60,
    )

    service = CarNoticeService(FakeStaffRepo(), real_punch_repo)

    assert service.notice_for(STAFF_PLATE, "ALLOW", "ENTRY") is None
