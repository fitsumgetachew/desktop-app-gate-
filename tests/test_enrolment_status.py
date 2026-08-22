"""How much of the roster this station can recognise, and how it says so.

The sync already logs "staff X has no usable face photo" on every cycle, but a
log file in a guard booth is nobody's dashboard — the first anyone notices is a
panel that says "Not recognised" forever, which reads as a broken camera rather
than an empty roster. These tests pin the wording that fixes that, because each
message has to point at where the problem actually is.

Pure — no Qt, no database.
"""

import sqlite3

import pytest

from smart_gate.repositories.db import init_db
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.services.enrolment_status import (
    LEVEL_NEUTRAL,
    LEVEL_OK,
    LEVEL_WARN,
    StaffEnrolment,
    from_rows,
    headline,
    summarise,
)


def _person(uid="u1", name="Abebe Bekele", photos=5, embedded=5, plates=1, pending=0):
    return StaffEnrolment(
        uid, name, photos, embedded, plates, pending_count=pending
    )


# ── One person's state ────────────────────────────────────────────────


def test_a_fully_enrolled_person_is_ready():
    person = _person()

    assert person.recognisable is True
    assert person.status_text == "5 ready"
    assert person.level == LEVEL_OK


def test_a_partially_usable_person_is_still_recognisable_but_flagged():
    """Four of five photos encoding is the normal case — a profile shot
    routinely yields no face — so this must not read as failure."""
    person = _person(photos=5, embedded=4)

    assert person.recognisable is True
    assert person.status_text == "4 of 5 usable"
    assert person.level == LEVEL_WARN


def test_a_person_whose_photos_all_failed_cannot_be_recognised():
    person = _person(photos=3, embedded=0)

    assert person.recognisable is False
    assert person.status_text == "3 photos, none usable"


def test_a_person_with_plates_but_no_photos_is_a_normal_state_not_a_fault():
    """Membership is >=1 photo OR >=1 plate, so a plates-only staff member is
    legitimately on the roster. They cannot be recognised by face — and do not
    need to be: their plate still drives the car-without-attendance notice, and
    a gate with no face camera never wanted their photo. Flagging it as a
    warning trained the guard to ignore the one state that IS a fault."""
    person = _person(photos=0, embedded=0, plates=1)

    assert person.recognisable is False
    assert person.status_text == "Plates only — no face enrolment"
    assert person.level == LEVEL_NEUTRAL


def test_a_person_still_being_enrolled_is_not_reported_as_broken():
    """Photos arrive a few per cycle, so "0 of 5 embedded" is the normal state
    of a fresh roster for a while."""
    person = _person(photos=5, embedded=2, pending=3)

    assert person.status_text == "Enrolling… 2/5"
    assert person.level == LEVEL_NEUTRAL


def test_photos_that_all_failed_is_still_a_warning_once_settled():
    """The one case somebody must actually fix."""
    person = _person(photos=3, embedded=0, pending=0)

    assert person.status_text == "3 photos, none usable"
    assert person.level == LEVEL_WARN


# ── Summary ───────────────────────────────────────────────────────────


def test_summarise_counts_across_everyone():
    summary = summarise([_person("a", photos=5, embedded=4), _person("b", photos=2, embedded=0)])

    assert summary.staff_total == 2
    assert summary.photos_total == 7
    assert summary.embedded_total == 4
    assert summary.ready_staff == 1


def test_portal_sent_no_photos_is_only_true_when_staff_arrived_without_any():
    assert summarise([_person(photos=0, embedded=0)]).portal_sent_no_photos is True
    assert summarise([_person(photos=1, embedded=0)]).portal_sent_no_photos is False
    assert summarise([]).portal_sent_no_photos is False


def test_an_empty_roster_summarises_to_zeroes():
    summary = summarise([])

    assert summary.staff_total == 0
    assert summary.any_recognisable is False


# ── The headline ──────────────────────────────────────────────────────


def test_nothing_synced_yet_is_neutral_not_an_error():
    text, level = headline(summarise([]))

    assert level == LEVEL_NEUTRAL
    assert "No staff synced" in text


def test_staff_without_photos_names_the_portal_as_the_place_to_fix_it():
    """This is the failure that otherwise looks like a broken camera. It has to
    say where the fix lives, because it is not on this machine."""
    text, level = headline(summarise([_person(photos=0, embedded=0)]))

    assert level == LEVEL_WARN
    assert "no photos" in text
    assert "portal" in text


def test_photos_that_all_failed_to_encode_is_a_different_message():
    """Downloaded-but-unusable is a different problem from never-sent, and
    conflating them would send someone to look in the wrong place."""
    text, level = headline(summarise([_person(photos=4, embedded=0)]))

    assert level == LEVEL_WARN
    assert "none usable" in text
    assert "no photos" not in text.lower().replace("none usable", "")


def test_a_partly_ready_roster_reports_how_many_cannot_be_recognised():
    text, level = headline(
        summarise([_person("a", photos=5, embedded=5), _person("b", photos=0, embedded=0)])
    )

    assert level == LEVEL_WARN
    assert "1 of 2 staff ready" in text
    assert "1 cannot be recognised" in text


def test_a_fully_ready_roster_is_ok_and_states_the_numbers():
    text, level = headline(
        summarise([_person("a", photos=5, embedded=5), _person("b", photos=3, embedded=3)])
    )

    assert level == LEVEL_OK
    assert "2 staff ready" in text
    assert "8 photos embedded" in text


# ── The query behind it ───────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield StaffRepository(conn)
    conn.close()


def test_enrolment_rows_counts_photos_embeddings_and_plates(repo):
    repo.upsert_staff("u1", "Abebe Bekele", 1, 1)
    repo.replace_plates("u1", ["AA12345", "BB99887"])
    repo.upsert_photo("u1", 1, "h1", b"\x00" * 1024, 100)     # usable
    repo.upsert_photo("u1", 2, "h2", None, 100)               # no face found

    staff = from_rows(repo.enrolment_rows())

    assert len(staff) == 1
    assert staff[0].full_name == "Abebe Bekele"
    assert staff[0].photo_count == 2
    assert staff[0].embedded_count == 1
    assert staff[0].plate_count == 2


def test_a_staff_member_with_no_photos_still_appears(repo):
    """The live portal case — they must be listed, not silently absent."""
    repo.upsert_staff("u1", "Fitsum Tola Tola", 1, 1)

    staff = from_rows(repo.enrolment_rows())

    assert [s.full_name for s in staff] == ["Fitsum Tola Tola"]
    assert staff[0].photo_count == 0
    assert summarise(staff).portal_sent_no_photos is True


def test_rows_come_back_sorted_by_name(repo):
    repo.upsert_staff("u2", "Sara Tesfaye", 1, 1)
    repo.upsert_staff("u1", "abebe bekele", 1, 1)

    assert [s.full_name for s in from_rows(repo.enrolment_rows())] == [
        "abebe bekele",
        "Sara Tesfaye",
    ]


def test_a_staff_member_with_no_name_falls_back_to_the_uid(repo):
    repo.upsert_staff("u1", "", 1, 1)

    assert from_rows(repo.enrolment_rows())[0].full_name == "u1"


def test_an_empty_roster_yields_no_rows(repo):
    assert from_rows(repo.enrolment_rows()) == []
