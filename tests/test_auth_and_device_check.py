"""Device-check fail-closed behaviour, and the access-token lifecycle."""

import time
from types import SimpleNamespace

import pytest
import requests

from smart_gate.services.auth_service import AuthService, SessionExpiredError
from smart_gate.services.token_store import TokenStore


# ── Device check: fail closed on an explicit refusal, open on a network error ──


def _device():
    return SimpleNamespace(device_id="dev-1")


_NO_DEVICE = object()


def _login_worker_check(api, device=_NO_DEVICE):
    # Imported lazily: smart_gate.main pulls in Qt widgets.
    from smart_gate.main import LoginWorker

    return LoginWorker._check_device(
        api, "token", _device() if device is _NO_DEVICE else device
    )


class _CheckApi:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def check_device(self, token, device_id):
        if self.error:
            raise self.error
        return self.result


def test_registered_device_is_accepted_with_its_assignment():
    api = _CheckApi({
        "registered": True,
        "gate": {"id": "GATE-9"},
        "lane": {"id": "LANE-Z"},
    })
    result = _login_worker_check(api)
    assert result["registered"] is True
    assert result["offline"] is False
    assert result["gate_id"] == "GATE-9"
    assert result["lane_id"] == "LANE-Z"


def test_unregistered_device_is_refused_not_defaulted_to_true():
    """The bug: a failed check defaulted device_registered to True."""
    api = _CheckApi({"registered": False, "message": "Device not registered."})
    result = _login_worker_check(api)
    assert result["registered"] is False
    assert result["offline"] is False


def test_network_error_yields_offline_mode_not_a_refusal():
    api = _CheckApi(error=requests.ConnectionError("no route to host"))
    result = _login_worker_check(api)
    assert result["registered"] is None   # unknown, not False → login proceeds
    assert result["offline"] is True


def test_missing_local_device_is_offline_not_refused():
    api = _CheckApi({"registered": True})
    result = _login_worker_check(api, device=None)
    assert result["registered"] is None
    assert result["offline"] is True


def test_device_binding_mismatch_is_named_not_hidden_behind_offline_mode():
    """The portal answers 403 when a session's device_id is not this machine's.

    Sliding into offline mode would bury a condition the operator can fix in
    thirty seconds by signing in again from this machine.
    """
    api = _CheckApi(error=_http_error(403))
    result = _login_worker_check(api)
    assert result["registered"] is False      # fail closed → back to sign-in
    assert result["offline"] is False         # not "the server is down"
    assert "different device" in result["message"]


@pytest.mark.parametrize("status", [404, 501])
def test_a_server_without_device_check_still_lets_the_gate_work(status):
    api = _CheckApi(error=_http_error(status))
    result = _login_worker_check(api)
    assert result["registered"] is None
    assert result["offline"] is True
    assert "not available" in result["message"]


def test_server_names_are_passed_through_for_the_operator_to_confirm():
    api = _CheckApi({
        "registered": True,
        "gate": {"id": "GATE-9", "name": "Main Gate"},
        "lane": {"id": "LANE-Z", "name": "Entry Lane"},
    })
    result = _login_worker_check(api)
    assert result["gate_name"] == "Main Gate"
    assert result["lane_name"] == "Entry Lane"


# ── Token store ───────────────────────────────────────────────────────


def test_token_store_reports_when_a_refresh_is_due():
    store = TokenStore()
    store.set_token("tok", expires_in=900)
    assert store.needs_refresh() is False
    # Pretend 80% of the TTL has elapsed.
    store._issued_at = time.time() - 721
    assert store.needs_refresh() is True


def test_empty_token_store_never_asks_for_a_refresh():
    store = TokenStore()
    assert store.needs_refresh() is False
    assert store.has_token() is False


def test_clear_drops_the_token():
    store = TokenStore()
    store.set_token("tok")
    store.clear()
    assert store.get_token() is None


# ── Refresh-and-retry ─────────────────────────────────────────────────


class _Repo:
    def __init__(self, refresh_token="refresh-1"):
        self.device = SimpleNamespace(device_id="dev-1", refresh_token=refresh_token)
        self.saved_refresh = None

    def get_device(self):
        return self.device

    def update_refresh_token(self, device_id, token):
        self.saved_refresh = token


class _RefreshApi:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def refresh(self, refresh_token):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code}", response=response)


def test_call_authed_refreshes_and_retries_once_on_401():
    store = TokenStore()
    store.set_token("stale")
    auth = AuthService(_RefreshApi({"access_token": "fresh", "expires_in": 900}),
                       _Repo(), tokens=store)

    seen = []

    def call(token):
        seen.append(token)
        if token == "stale":
            raise _http_error(401)
        return {"ok": True}

    assert auth.call_authed(call) == {"ok": True}
    assert seen == ["stale", "fresh"]


def test_call_authed_raises_session_expired_when_refresh_fails():
    """"Refresh failed" now means the server *rejected the refresh token* — a
    429 or a dropped connection is transient and must not end the session (see
    test_deprovision_and_ratelimit.py)."""
    store = TokenStore()
    store.set_token("stale")
    auth = AuthService(_RefreshApi(error=_http_error(401)), _Repo(), tokens=store)

    with pytest.raises(SessionExpiredError):
        auth.call_authed(lambda token: (_ for _ in ()).throw(_http_error(401)))


def test_call_authed_does_not_retry_non_401_errors():
    store = TokenStore()
    store.set_token("tok")
    api = _RefreshApi({"access_token": "fresh"})
    auth = AuthService(api, _Repo(), tokens=store)

    with pytest.raises(requests.HTTPError):
        auth.call_authed(lambda token: (_ for _ in ()).throw(_http_error(500)))
    assert api.calls == 0


def test_refresh_persists_a_rotated_refresh_token():
    store = TokenStore()
    repo = _Repo()
    auth = AuthService(
        _RefreshApi({"access_token": "fresh", "refresh_token": "refresh-2"}),
        repo,
        tokens=store,
    )
    assert auth.refresh_access_token() is True
    assert store.get_token() == "fresh"
    assert repo.saved_refresh == "refresh-2"


def test_refresh_without_a_stored_refresh_token_fails_cleanly():
    auth = AuthService(_RefreshApi({}), _Repo(refresh_token=None), tokens=TokenStore())
    assert auth.refresh_access_token() is False


def test_ensure_fresh_token_refreshes_proactively():
    store = TokenStore()
    store.set_token("old", expires_in=900)
    store._issued_at = time.time() - 800   # 89% of the TTL burned
    api = _RefreshApi({"access_token": "fresh", "expires_in": 900})

    token = AuthService(api, _Repo(), tokens=store).ensure_fresh_token()

    assert api.calls == 1
    assert token == "fresh"


def test_ensure_fresh_token_leaves_a_young_token_alone():
    store = TokenStore()
    store.set_token("young", expires_in=900)
    api = _RefreshApi({"access_token": "fresh"})

    token = AuthService(api, _Repo(), tokens=store).ensure_fresh_token()

    assert api.calls == 0
    assert token == "young"
