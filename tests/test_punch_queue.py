"""The attendance outbox: suppression, the drain, and the retry cap.

Mirrors ``test_repos``/``test_sync_health`` in style — a real SQLite file, a
stub ApiClient, no Qt event loop and no network. The drain is
``SyncWorker._push_punches_batch`` driven on an un-started worker.
"""

import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from smart_gate.repositories.db import init_db
from smart_gate.repositories.punch_repo import (
    MAX_SYNC_ATTEMPTS,
    PunchRepository,
    local_day_start,
)
from smart_gate.services.attendance_service import (
    PUNCH_SUPPRESSION_SECONDS,
    AttendanceService,
)
from smart_gate.services.face_recognition_service import FaceMatch
from smart_gate.services.sync_service import SyncWorker
from smart_gate.utils.config import load_config

MATCH = FaceMatch(staff_uid="stf-0001", full_name="Abebe Bekele", confidence=75.5, distance=0.245)
OTHER = FaceMatch(staff_uid="stf-0002", full_name="Sara Tesfaye", confidence=70.0, distance=0.30)


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "test.db")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn):
    return PunchRepository(conn)


@pytest.fixture
def service(repo):
    return AttendanceService(repo, device_id="dev-1", gate_id="GATE-1", lane_id="LANE-A")


# ── Recording ─────────────────────────────────────────────────────────


def test_a_recognised_face_writes_a_punch(service, repo):
    outcome = service.record_punch(MATCH, punch_time=1_000_000)

    assert outcome.recorded is True
    row = repo.list_recent()[0]
    assert row["staff_uid"] == "stf-0001"
    assert row["method"] == "face"
    assert row["confidence"] == pytest.approx(75.5)
    assert row["device_id"] == "dev-1"
    assert row["gate_id"] == "GATE-1"
    assert row["lane_id"] == "LANE-A"
    assert row["synced"] == 0
    assert row["sync_attempts"] == 0


def test_every_punch_gets_its_own_uuid_idempotency_key(service, repo):
    first = service.record_punch(MATCH, punch_time=1_000_000)
    second = service.record_punch(MATCH, punch_time=1_000_000 + PUNCH_SUPPRESSION_SECONDS)

    assert first.punch.id != second.punch.id
    assert len({row["id"] for row in repo.list_recent()}) == 2


# ── Suppression ───────────────────────────────────────────────────────


def test_the_suppression_window_is_five_minutes():
    assert PUNCH_SUPPRESSION_SECONDS == 300


def test_a_second_match_inside_the_window_writes_nothing(service, repo):
    """Someone standing in front of the camera is recognised ~3 times a second;
    without this they would file hundreds of punches."""
    service.record_punch(MATCH, punch_time=1_000_000)

    outcome = service.record_punch(MATCH, punch_time=1_000_000 + 299)

    assert outcome.recorded is False
    assert outcome.suppressed is True
    assert outcome.suppressed_since == 1_000_000
    assert len(repo.list_recent()) == 1


def test_a_match_after_the_window_writes_again(service, repo):
    service.record_punch(MATCH, punch_time=1_000_000)

    outcome = service.record_punch(MATCH, punch_time=1_000_000 + 300)

    assert outcome.recorded is True
    assert len(repo.list_recent()) == 2


def test_suppression_is_per_staff_member(service, repo):
    service.record_punch(MATCH, punch_time=1_000_000)

    assert service.record_punch(OTHER, punch_time=1_000_001).recorded is True


def test_suppression_counts_punches_that_already_synced(service, repo):
    """A punch the portal has acknowledged is exactly the one that must stop the
    next thirty frames — so the check ignores sync state entirely."""
    first = service.record_punch(MATCH, punch_time=1_000_000)
    repo.mark_synced(first.punch.id)

    assert service.record_punch(MATCH, punch_time=1_000_100).recorded is False


def test_synced_punches_are_never_deleted(service, repo):
    """Both suppression and the daily counters read this table, so a row still
    has work to do after synced=1."""
    outcome = service.record_punch(MATCH, punch_time=int(time.time()))
    repo.mark_synced(outcome.punch.id)

    assert len(repo.list_recent()) == 1
    assert repo.punches_today("stf-0001") == 1


# ── Daily counters (local calendar day) ───────────────────────────────


def test_today_is_the_local_calendar_day_not_utc(repo, service):
    """A gate in UTC+3 must not reset its counters at 3 a.m."""
    midnight = local_day_start()
    assert datetime.fromtimestamp(midnight).hour == 0
    assert datetime.fromtimestamp(midnight).minute == 0

    service.record_punch(MATCH, punch_time=midnight + 60)
    service.record_punch(OTHER, punch_time=midnight + 61)
    service.record_punch(MATCH, punch_time=midnight - 60)     # yesterday, late

    assert repo.punches_today("stf-0001") == 1
    assert repo.punch_count_today() == 2
    assert repo.staff_punched_today() == 2


def test_local_day_start_tracks_the_day_it_is_given():
    yesterday = datetime.now() - timedelta(days=1)

    assert local_day_start(yesterday.timestamp()) == int(
        yesterday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )


def test_counters_are_zero_on_a_fresh_gate(repo):
    assert repo.punches_today("stf-0001") == 0
    assert repo.punch_count_today() == 0


# ── The drain ─────────────────────────────────────────────────────────


class StubApi:
    """Answers /attendance/batch per item, like /events/batch does."""

    def __init__(self, decide=None, error=None):
        self.batches = []
        self.error = error
        self.decide = decide or (lambda item: {"ok": True, "deduped": False})

    def post_attendance_batch(self, token, items):
        if self.error:
            raise self.error
        self.batches.append(items)
        return {
            "ok": True,
            "results": [dict(self.decide(item), id=item["id"]) for item in items],
        }


@pytest.fixture
def drain_worker(tmp_path, monkeypatch, repo):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    w = SyncWorker(config=load_config(), db_path=tmp_path / "test.db", interval_seconds=10)
    w.punch_repo = repo
    return w


def test_the_drain_sends_the_client_id_as_the_idempotency_key(drain_worker, service, repo):
    outcome = service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi()

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    sent = drain_worker.api.batches[0][0]
    assert sent["id"] == outcome.punch.id
    assert sent["staff_uid"] == "stf-0001"
    assert sent["method"] == "face"
    assert sent["punch_time"] == 1_000_000
    assert repo.list_unsynced() == []


def test_a_deduped_punch_counts_as_delivered(drain_worker, service, repo):
    """`deduped: true` means the portal already had this id — the punch landed,
    so retrying it forever would be wrong."""
    service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi(decide=lambda item: {"ok": True, "deduped": True})

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    assert repo.list_unsynced() == []
    assert repo.list_recent()[0]["synced"] == 1


def test_ids_are_preserved_across_a_redelivery(drain_worker, service, repo):
    """A punch left unsynced by a dropped response must go back with the *same*
    id, so the server can dedupe it rather than double-count attendance."""
    outcome = service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi(decide=lambda item: {"ok": False, "error": "later"})
    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    drain_worker.api = StubApi()
    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    assert [batch[0]["id"] for batch in drain_worker.api.batches] == [outcome.punch.id]
    assert repo.list_recent()[0]["synced"] == 1


def test_a_rejected_punch_increments_its_attempt_counter(drain_worker, service, repo):
    service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi(decide=lambda item: {"ok": False, "error": "bad staff_uid"})

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    row = repo.list_recent()[0]
    assert row["sync_attempts"] == 1
    assert row["synced"] == 0
    assert row["last_sync_error"] == "bad staff_uid"


def test_a_punch_missing_from_the_results_stays_queued_untouched(drain_worker, service, repo):
    service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi()
    drain_worker.api.post_attendance_batch = lambda token, items: {"results": []}

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    row = repo.list_recent()[0]
    assert row["synced"] == 0
    assert row["sync_attempts"] == 0      # not a rejection, so not an attempt


def test_a_punch_stops_being_retried_at_the_attempt_cap(drain_worker, service, repo):
    service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi(decide=lambda item: {"ok": False, "error": "nope"})

    for _ in range(MAX_SYNC_ATTEMPTS + 3):
        pending = repo.list_unsynced()
        if not pending:
            break
        drain_worker._push_punches_batch("tok", pending)

    assert repo.list_unsynced() == []                       # given up on
    assert repo.list_recent()[0]["sync_attempts"] == MAX_SYNC_ATTEMPTS
    assert repo.list_recent()[0]["synced"] == 0


def test_one_bad_punch_does_not_hold_up_the_others(drain_worker, service, repo):
    service.record_punch(MATCH, punch_time=1_000_000)
    service.record_punch(OTHER, punch_time=1_000_001)
    drain_worker.api = StubApi(
        decide=lambda item: {"ok": item["staff_uid"] != "stf-0002", "error": "nope"}
    )

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    remaining = repo.list_unsynced()
    assert [row["staff_uid"] for row in remaining] == ["stf-0002"]


def test_the_drain_sends_oldest_first(drain_worker, service, repo):
    service.record_punch(OTHER, punch_time=1_000_500)
    service.record_punch(MATCH, punch_time=1_000_000)
    drain_worker.api = StubApi()

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    assert [item["punch_time"] for item in drain_worker.api.batches[0]] == [
        1_000_000,
        1_000_500,
    ]


@pytest.mark.parametrize("id_key", ["id", "punch_id", "event_id"])
def test_the_drain_accepts_whichever_id_key_the_portal_settles_on(
    drain_worker, service, repo, id_key
):
    """The endpoint is being built in parallel; keying results off one guessed
    field name would silently leave every punch queued forever."""
    service.record_punch(MATCH, punch_time=1_000_000)
    api = StubApi()
    api.post_attendance_batch = lambda token, items: {
        "results": [{id_key: item["id"], "ok": True} for item in items]
    }
    drain_worker.api = api

    drain_worker._push_punches_batch("tok", repo.list_unsynced())

    assert repo.list_unsynced() == []
