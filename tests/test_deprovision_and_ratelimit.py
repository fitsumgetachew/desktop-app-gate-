"""De-provisioning detection, and rate limiting that must not sign anyone out.

Two failure modes that look similar on the wire but mean opposite things:

* heartbeat 404 in portal mode — the portal deleted this device's record, so the
  session is over and the machine must stop operating on its cache;
* 429 from ``/auth/refresh`` — the server is merely busy; the session is fine and
  the only correct response is to try again later.
"""

from types import SimpleNamespace

import pytest
import requests

from smart_gate.services.auth_service import (
    AuthService,
    SessionExpiredError,
    TransientAuthError,
    refresh_coordinator,
)
from smart_gate.services.sync_service import DEPROVISIONED_MESSAGE, SyncWorker
from smart_gate.services.token_store import TokenStore
from smart_gate.utils.config import AUTH_MODE_MOCK, AUTH_MODE_PORTAL, load_config


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code}", response=response)


# ── De-provisioning ───────────────────────────────────────────────────


class _HeartbeatApi:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def heartbeat(self, token, payload):
        self.calls += 1
        if self.error:
            raise self.error
        return {"ok": True}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    return SyncWorker(config=load_config(), db_path=tmp_path / "t.db", interval_seconds=10)


def _emitted(worker):
    """Collect device_deprovisioned emissions without a Qt event loop."""
    seen = []
    worker.device_deprovisioned.connect(seen.append)
    return seen


def test_heartbeat_404_in_portal_mode_reports_deprovisioning(worker):
    worker._config.auth_mode = AUTH_MODE_PORTAL
    worker.api = _HeartbeatApi(error=_http_error(404))
    seen = _emitted(worker)

    worker._send_heartbeat("tok", "dev-1")

    assert seen == [DEPROVISIONED_MESSAGE]


def test_heartbeat_404_in_mock_mode_stays_a_soft_fail(worker):
    """The reference server 404s heartbeats for any unregistered device during
    ordinary dev flows — acting on that would sign developers out constantly."""
    worker._config.auth_mode = AUTH_MODE_MOCK
    worker.api = _HeartbeatApi(error=_http_error(404))
    seen = _emitted(worker)

    worker._send_heartbeat("tok", "dev-1")

    assert seen == []


@pytest.mark.parametrize("status", [500, 503])
def test_other_heartbeat_errors_never_deprovision(worker, status):
    worker._config.auth_mode = AUTH_MODE_PORTAL
    worker.api = _HeartbeatApi(error=_http_error(status))
    seen = _emitted(worker)

    worker._send_heartbeat("tok", "dev-1")

    assert seen == []


def test_a_deprovisioning_heartbeat_is_not_recorded_as_delivered(worker):
    """The health flag must not advance on a heartbeat the server refused."""
    worker._config.auth_mode = AUTH_MODE_PORTAL
    worker.api = _HeartbeatApi(error=_http_error(404))

    worker._send_heartbeat("tok", "dev-1")

    assert worker._last_reported_health is None


# ── Rate limiting ─────────────────────────────────────────────────────


class _Repo:
    def __init__(self):
        self.device = SimpleNamespace(device_id="dev-1", refresh_token="refresh-1")
        self.saved_refresh = None

    def get_device(self):
        return self.device

    def update_refresh_token(self, device_id, token):
        self.saved_refresh = token
        self.device.refresh_token = token


class _RefreshApi:
    def __init__(self, error=None, response=None):
        self.error = error
        self.response = response
        self.calls = 0

    def refresh(self, refresh_token):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def _auth(api, repo=None, token="old-token"):
    store = TokenStore()
    if token:
        store.set_token(token, expires_in=900)
    return AuthService(api, repo or _Repo(), tokens=store), store


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_busy_server_never_ends_the_session(status):
    """429 says "slow down", not "your token is invalid"."""
    repo = _Repo()
    auth, store = _auth(_RefreshApi(error=_http_error(status)), repo)

    with pytest.raises(TransientAuthError):
        auth.refresh_access_token()

    assert store.get_token() == "old-token"      # session intact
    assert repo.device.refresh_token == "refresh-1"


def test_a_dropped_connection_during_refresh_is_transient_too():
    auth, store = _auth(_RefreshApi(error=requests.ConnectionError("boom")))

    with pytest.raises(TransientAuthError):
        auth.refresh_access_token()

    assert store.get_token() == "old-token"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_refresh_token_does_end_the_session(status):
    """The one case where the session really is over."""
    auth, _ = _auth(_RefreshApi(error=_http_error(status)))

    assert auth.refresh_access_token() is False


def test_rate_limiting_never_forces_re_login_through_call_authed():
    """A 401 whose refresh is rate limited must not surface as SessionExpired —
    that would bounce the operator to the sign-in screen mid-shift."""
    auth, _ = _auth(_RefreshApi(error=_http_error(429)))

    def _always_401(token):
        raise _http_error(401)

    with pytest.raises(TransientAuthError):
        auth.call_authed(_always_401)


def test_a_genuinely_expired_session_still_raises_session_expired():
    auth, _ = _auth(_RefreshApi(error=_http_error(401)))

    def _always_401(token):
        raise _http_error(401)

    with pytest.raises(SessionExpiredError):
        auth.call_authed(_always_401)


def test_proactive_refresh_keeps_the_current_token_when_rate_limited():
    """ensure_fresh_token must hand back the still-valid token, not None: the
    cycle should carry on with the ~20% of TTL it has left."""
    api = _RefreshApi(error=_http_error(429))
    auth, store = _auth(api)
    store._issued_at -= 800          # push past the 80% refresh threshold

    token = auth.ensure_fresh_token(0.8)

    assert token == "old-token"
    assert api.calls == 1


def test_the_sync_loop_treats_a_rate_limited_refresh_as_retryable(worker):
    """SyncWorker must back off rather than emit auth_required."""
    marker = refresh_coordinator.marker()
    worker.auth = AuthService(
        _RefreshApi(error=_http_error(429)), _Repo(), tokens=TokenStore()
    )
    worker.auth.tokens.set_token("old-token", expires_in=900)

    with pytest.raises(TransientAuthError):
        worker._try_refresh_token(marker)
