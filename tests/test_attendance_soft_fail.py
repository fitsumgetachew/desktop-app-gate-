"""Attendance must never take the barrier offline.

The contrast with ``tests/test_sync_health.py`` is the whole point of this file.
There, a failing allowlist pull is *deferred and re-raised*: the portal is told,
and then the app drops offline and backs off, because a gate that cannot refresh
its allowlist is genuinely degraded.

Attendance is the station's second job. A roster or punch-drain failure colours
the heartbeat DEGRADED and nothing else — the cycle finishes, ``_sync_once``
returns True, and the guard's barrier keeps working. Taking a gate offline
because a staff photo would not download is exactly backwards.

Driven directly on an un-started ``SyncWorker``: no Qt event loop, no network.
"""

from types import SimpleNamespace

import pytest
import requests

from smart_gate.services.sync_service import HEALTH_DEGRADED, HEALTH_OK, SyncWorker
from smart_gate.utils.config import load_config


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code}", response=response)


class _Api:
    """A healthy gate cycle, with the two attendance calls made to fail on demand."""

    def __init__(self, roster_error=None, attendance_error=None):
        self.roster_error = roster_error
        self.attendance_error = attendance_error
        self.heartbeats = []
        self.attendance_batches = []

    def get_allowlist(self, token, since_version):
        return {"version": "1000", "items": [], "deleted": []}

    def get_manual_reasons(self, token):
        return {"items": []}

    def get_staff_roster(self, token, since_version):
        if self.roster_error:
            raise self.roster_error
        return {"version": "1000", "items": [], "deleted": []}

    def post_attendance_batch(self, token, items):
        if self.attendance_error:
            raise self.attendance_error
        self.attendance_batches.append(items)
        return {"results": [{"id": item["id"], "ok": True} for item in items]}

    def heartbeat(self, token, payload):
        self.heartbeats.append(payload)
        return {"ok": True}


def _punch_row(punch_id="p-1"):
    return {
        "id": punch_id,
        "staff_uid": "stf-0001",
        "punch_time": 1_000_000,
        "method": "face",
        "confidence": 75.5,
        "device_id": "dev-1",
        "gate_id": "GATE-1",
        "lane_id": "LANE-A",
    }


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    w = SyncWorker(config=load_config(), db_path=tmp_path / "t.db", interval_seconds=10)
    w.device_repo = SimpleNamespace(get_device=lambda: SimpleNamespace(device_id="dev-1"))
    w.allow_repo = SimpleNamespace(
        get_last_version=lambda: 900,
        upsert_records=lambda items: None,
        delete_plates=lambda plates: 0,
        replace_all=lambda items: None,
    )
    w.reason_repo = SimpleNamespace(replace_all=lambda items: None)
    w.event_repo = SimpleNamespace(
        list_unsynced=lambda: [],
        list_pending_evidence_upload=lambda: [],
    )
    w.presence_repo = SimpleNamespace(upsert_presence=lambda *a: None)
    w.auth = SimpleNamespace(ensure_fresh_token=lambda ratio: "tok")
    w.staff_repo = SimpleNamespace(
        get_last_version=lambda: 500,
        list_staff_uids=lambda: [],
        delete_staff=lambda uids: 0,
        list_encodings=lambda: [],
        # Photos are fetched by the paced queue, which this fixture keeps empty:
        # these tests are about how the *cycle* reports health, not about photos.
        pending_photos=lambda limit, max_attempts: [],
        photo_queue_progress=lambda: (0, 0),
        queue_photo=lambda *a: None,
    )
    w.punch_repo = SimpleNamespace(
        list_unsynced=lambda limit=200: [],
        mark_synced=lambda punch_id: None,
        increment_sync_attempt=lambda punch_id, error: None,
    )
    return w


# ── Roster ────────────────────────────────────────────────────────────


def test_a_clean_cycle_with_attendance_still_reports_ok(worker):
    worker.api = _Api()

    assert worker._sync_once() is True

    assert worker.api.heartbeats[0]["status"] == HEALTH_OK
    assert worker.api.heartbeats[0]["last_error"] is None


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_a_roster_failure_degrades_the_heartbeat_without_raising(worker, status):
    """Contrast with test_sync_health's allowlist tests, which assert a re-raise."""
    worker.api = _Api(roster_error=_http_error(status))

    assert worker._sync_once() is True          # cycle completes, gate stays online

    beat = worker.api.heartbeats[0]
    assert beat["status"] == HEALTH_DEGRADED
    assert beat["last_error"] == f"staff roster sync failed: HTTP {status}"


def test_a_roster_connection_error_is_reported_by_class_name(worker):
    worker.api = _Api(roster_error=requests.ConnectionError("no route"))

    assert worker._sync_once() is True

    assert (
        worker.api.heartbeats[0]["last_error"]
        == "staff roster sync failed: ConnectionError"
    )


def test_an_unexpected_roster_error_is_contained_too(worker):
    """Not just HTTP: a malformed response or a disk problem inside the roster
    step must not reach the run loop either."""
    worker.api = _Api(roster_error=ValueError("bad json"))

    assert worker._sync_once() is True

    assert worker.api.heartbeats[0]["last_error"] == "staff roster sync failed: ValueError"


def test_the_allowlist_still_re_raises_while_the_roster_does_not(worker):
    """Both fail in the same cycle: the allowlist decides the app's fate, the
    roster only ever decides the heartbeat's colour."""
    api = _Api(roster_error=_http_error(503))
    api.get_allowlist = lambda token, since_version: (_ for _ in ()).throw(
        _http_error(500)
    )
    worker.api = api

    with pytest.raises(requests.HTTPError):
        worker._sync_once()

    # The allowlist error was recorded first and is the one the operator sees.
    assert api.heartbeats[0]["last_error"] == "allowlist sync failed: HTTP 500"


def test_the_roster_step_is_skipped_when_attendance_is_disabled(worker):
    worker._config.face_attendance_enabled = False
    api = _Api(roster_error=_http_error(500))
    worker.api = api

    assert worker._sync_once() is True

    assert api.heartbeats[0]["status"] == HEALTH_OK


def test_a_gate_without_a_staff_repo_skips_attendance_entirely(worker):
    """An older database, or a worker constructed before run() opened its
    repositories, must not break the cycle."""
    worker.staff_repo = None
    worker.punch_repo = None
    worker.api = _Api()

    assert worker._sync_once() is True
    assert worker.api.heartbeats[0]["status"] == HEALTH_OK


# ── Punch drain ───────────────────────────────────────────────────────


def test_an_attendance_failure_degrades_the_heartbeat_without_raising(worker):
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    worker.api = _Api(attendance_error=_http_error(502))

    assert worker._sync_once() is True

    beat = worker.api.heartbeats[0]
    assert beat["status"] == HEALTH_DEGRADED
    assert beat["last_error"] == "attendance sync failed: HTTP 502"


def test_the_punch_drain_runs_on_a_healthy_cycle(worker):
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    synced = []
    worker.punch_repo.mark_synced = synced.append
    worker.api = _Api()

    assert worker._sync_once() is True

    assert worker.api.attendance_batches == [[_punch_row()]]
    assert synced == ["p-1"]


def test_an_attendance_failure_does_not_stop_the_heartbeat(worker):
    """The failure the operator most needs to see must not be the one that
    silences the gate."""
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    worker.api = _Api(attendance_error=requests.ConnectionError("down"))

    worker._sync_once()

    assert len(worker.api.heartbeats) == 1


def test_a_roster_failure_does_not_stop_the_punch_drain(worker):
    """Two independent soft-fails: one broken step must not skip the other."""
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    worker.api = _Api(roster_error=_http_error(503))

    assert worker._sync_once() is True

    assert worker.api.attendance_batches == [[_punch_row()]]


def test_the_first_failure_wins_the_last_error_slot(worker):
    """`last_error` is one short line on someone else's screen; the roster
    failure happens first in the cycle and is the one reported."""
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    worker.api = _Api(roster_error=_http_error(503), attendance_error=_http_error(502))

    worker._sync_once()

    assert worker.api.heartbeats[0]["last_error"] == "staff roster sync failed: HTTP 503"


# ── An endpoint the server has not shipped yet ────────────────────────


def test_a_404_roster_endpoint_does_not_degrade_the_gate_before_it_has_worked(worker):
    """The attendance endpoints are being built in parallel with this app. A
    health signal that reads DEGRADED on every gate for weeks is one nobody
    reads, and the reference server has never had these routes at all."""
    worker.api = _Api(roster_error=_http_error(404))

    assert worker._sync_once() is True

    assert worker.api.heartbeats[0]["status"] == HEALTH_OK
    assert worker.api.heartbeats[0]["last_error"] is None


def test_a_404_attendance_endpoint_leaves_the_punches_queued(worker):
    """Not deployed is not the same as lost: nothing is marked synced."""
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    synced = []
    worker.punch_repo.mark_synced = synced.append
    attempts = []
    worker.punch_repo.increment_sync_attempt = lambda punch_id, error: attempts.append(punch_id)
    worker.api = _Api(attendance_error=_http_error(404))

    assert worker._sync_once() is True

    assert worker.api.heartbeats[0]["status"] == HEALTH_OK
    assert synced == [] and attempts == []


def test_a_404_from_the_allowlist_still_degrades_and_re_raises(worker):
    """The exemption is scoped to attendance. The allowlist is the gate's own
    data, and a 404 there is a real problem."""
    api = _Api()
    api.get_allowlist = lambda token, since_version: (_ for _ in ()).throw(
        _http_error(404)
    )
    worker.api = api

    with pytest.raises(requests.HTTPError):
        worker._sync_once()

    assert api.heartbeats[0]["status"] == HEALTH_DEGRADED


@pytest.mark.parametrize("status", [400, 500, 502, 503])
def test_every_other_status_is_still_a_real_roster_failure(worker, status):
    worker.api = _Api(roster_error=_http_error(status))

    worker._sync_once()

    assert worker.api.heartbeats[0]["status"] == HEALTH_DEGRADED


def test_a_404_after_the_roster_endpoint_has_worked_is_a_real_fault(worker):
    """The exemption lasts only until the endpoint proves it exists.

    Otherwise the day after the portal ships, a path typo or a stale endpoint
    override would stop attendance syncing silently and forever — a 404 does not
    count a sync attempt, so MAX_SYNC_ATTEMPTS never trips either.
    """
    api = _Api()
    worker.api = api
    worker._sync_once()                          # cycle 1: the endpoint answers
    assert api.heartbeats[0]["status"] == HEALTH_OK

    api.roster_error = _http_error(404)          # cycle 2: now it 404s
    assert worker._sync_once() is True           # still soft-fail, still online

    assert api.heartbeats[-1]["status"] == HEALTH_DEGRADED
    assert api.heartbeats[-1]["last_error"] == "staff roster sync failed: HTTP 404"


def test_a_404_after_the_attendance_endpoint_has_worked_is_a_real_fault(worker):
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    api = _Api()
    worker.api = api
    worker._sync_once()                          # cycle 1: the batch lands
    assert api.attendance_batches == [[_punch_row()]]

    api.attendance_error = _http_error(404)      # cycle 2: now it 404s
    assert worker._sync_once() is True

    assert api.heartbeats[-1]["status"] == HEALTH_DEGRADED
    assert api.heartbeats[-1]["last_error"] == "attendance sync failed: HTTP 404"


def test_proof_is_per_endpoint_not_shared(worker):
    """A working roster must not vouch for an attendance endpoint that has never
    answered — they are separate routes and ship separately."""
    worker.punch_repo.list_unsynced = lambda limit=200: [_punch_row()]
    api = _Api(attendance_error=_http_error(404))
    worker.api = api

    worker._sync_once()                          # roster proves itself, attendance 404s

    assert api.heartbeats[-1]["status"] == HEALTH_OK

    api.roster_error = _http_error(404)          # the *proven* one now 404s
    worker._sync_once()

    assert api.heartbeats[-1]["last_error"] == "staff roster sync failed: HTTP 404"


def test_an_endpoint_that_never_answers_stays_exempt_across_cycles(worker):
    """A gate pointed at the reference server runs for months like this; it must
    not accumulate noise."""
    worker.api = _Api(roster_error=_http_error(404))

    for _ in range(5):
        assert worker._sync_once() is True

    assert all(beat["status"] == HEALTH_OK for beat in worker.api.heartbeats)
