"""Heartbeat health reporting: the gate must stay visible to the portal.

A failing allowlist pull used to unwind the whole sync cycle, so the heartbeat —
the last step — never ran and the portal could not tell a gate that is failing to
sync from one that is unplugged. These tests pin the fixed behaviour: the cycle
finishes, the heartbeat carries DEGRADED plus a short reason, and the original
error is still raised so the local offline banner and backoff are unchanged.

``SyncWorker._sync_once`` is driven directly on an un-started worker — no Qt event
loop, no network.
"""

from types import SimpleNamespace

import pytest
import requests

from smart_gate.services.sync_service import (
    HEALTH_DEGRADED,
    HEALTH_OK,
    SyncWorker,
)
from smart_gate.utils.config import load_config


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code}", response=response)


class _Api:
    """Records heartbeats; the allowlist call can be made to fail."""

    def __init__(self, allowlist_error=None, heartbeat_error=None):
        self.allowlist_error = allowlist_error
        self.heartbeat_error = heartbeat_error
        self.heartbeats = []

    def get_allowlist(self, token, since_version):
        if self.allowlist_error:
            raise self.allowlist_error
        return {"version": "1000", "items": [], "deleted": []}

    def get_manual_reasons(self, token):
        return {"items": []}

    def heartbeat(self, token, payload):
        self.heartbeats.append(payload)
        if self.heartbeat_error:
            raise self.heartbeat_error
        return {"ok": True}


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
    return w


# ── Clean cycle ───────────────────────────────────────────────────────


def test_clean_cycle_reports_ok(worker):
    """The happy-path payload is exactly what it has always been."""
    api = _Api()
    worker.api = api

    assert worker._sync_once() is True

    assert api.heartbeats == [{
        "device_id": "dev-1",
        "app_version": "2.0.0",
        "status": HEALTH_OK,
        "last_error": None,
    }]


# ── Degraded cycle ────────────────────────────────────────────────────


def test_allowlist_failure_still_sends_a_degraded_heartbeat(worker):
    """The whole point: a broken sync must not make the gate go silent."""
    api = _Api(allowlist_error=_http_error(404))
    worker.api = api

    with pytest.raises(requests.HTTPError):
        worker._sync_once()

    assert len(api.heartbeats) == 1
    beat = api.heartbeats[0]
    assert beat["status"] == HEALTH_DEGRADED
    assert beat["last_error"] == "allowlist sync failed: HTTP 404"


def test_allowlist_failure_is_re_raised_so_local_behaviour_is_unchanged(worker):
    """Reporting to the portal must not swallow the error locally: the run loop
    still needs it to drop offline and back off."""
    error = _http_error(500)
    worker.api = _Api(allowlist_error=error)

    with pytest.raises(requests.HTTPError) as excinfo:
        worker._sync_once()

    assert excinfo.value is error


def test_connection_error_is_reported_by_class_name(worker):
    worker.api = _Api(allowlist_error=requests.ConnectionError("boom"))

    with pytest.raises(requests.ConnectionError):
        worker._sync_once()

    assert worker.api.heartbeats[0]["last_error"] == "allowlist sync failed: ConnectionError"


def test_last_error_never_leaks_response_bodies_or_urls(worker):
    """last_error lands on someone else's screen — keep plate data and tokens out."""
    response = requests.Response()
    response.status_code = 403
    response._content = b'{"plate_number": "AAU12345", "token": "secret-token"}'
    response.url = "https://portal.example/api/gate/sync/allowlist?token=secret-token"

    message = SyncWorker._describe_failure(
        "allowlist sync", requests.HTTPError("failed", response=response)
    )

    assert message == "allowlist sync failed: HTTP 403"
    assert "AAU12345" not in message
    assert "secret-token" not in message


# ── Auth errors keep their own path ───────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_credential_errors_skip_the_heartbeat_and_propagate(worker, status):
    """401/403 mean "these credentials are wrong", not "this gate is unhealthy" —
    the run loop refreshes or forces re-login, and no heartbeat is worth sending."""
    api = _Api(allowlist_error=_http_error(status))
    worker.api = api

    with pytest.raises(requests.HTTPError):
        worker._sync_once()

    assert api.heartbeats == []


# ── Cadence ───────────────────────────────────────────────────────────


def test_health_change_forces_an_immediate_heartbeat(worker):
    """A gate that just broke must not stay silent for the rest of the
    5-cycle window."""
    api = _Api()
    worker.api = api
    worker._sync_once()                      # cycle 1: OK, reported
    assert len(api.heartbeats) == 1

    worker._sync_once()                      # cycle 2: still OK, stays quiet
    assert len(api.heartbeats) == 1

    api.allowlist_error = _http_error(404)   # cycle 3: broke → report at once
    with pytest.raises(requests.HTTPError):
        worker._sync_once()
    assert len(api.heartbeats) == 2
    assert api.heartbeats[-1]["status"] == HEALTH_DEGRADED


def test_recovery_is_reported_immediately_too(worker):
    api = _Api(allowlist_error=_http_error(404))
    worker.api = api
    with pytest.raises(requests.HTTPError):
        worker._sync_once()

    api.allowlist_error = None
    worker._sync_once()

    assert [b["status"] for b in api.heartbeats] == [HEALTH_DEGRADED, HEALTH_OK]
    assert api.heartbeats[-1]["last_error"] is None


def test_a_failed_heartbeat_is_retried_next_cycle(worker):
    """Only a heartbeat the server accepted counts as 'the portal knows'."""
    api = _Api(heartbeat_error=requests.ConnectionError("down"))
    worker.api = api

    worker._sync_once()
    assert len(api.heartbeats) == 1          # attempted, but not acknowledged

    api.heartbeat_error = None
    worker._sync_once()
    assert len(api.heartbeats) == 2          # retried rather than waiting 5 cycles


def test_manual_reasons_failure_degrades_without_failing_the_cycle(worker):
    """Manual reasons are soft-fail today and stay soft-fail — but the portal
    still gets told why the gate is unhappy."""
    api = _Api()

    def _boom(token):
        raise requests.ConnectionError("no route")

    api.get_manual_reasons = _boom
    worker.api = api

    assert worker._sync_once() is True       # cycle still succeeds
    assert api.heartbeats[0]["status"] == HEALTH_DEGRADED
    assert api.heartbeats[0]["last_error"] == "manual reasons sync failed: ConnectionError"
