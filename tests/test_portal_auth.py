"""Portal SSO mode: config, code entry, single-flight refresh, logout revoke."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from smart_gate.services.auth_service import (
    AuthService,
    normalize_one_time_code,
    refresh_coordinator,
)
from smart_gate.services.token_store import TokenStore
from smart_gate.utils.config import (
    AUTH_MODE_MOCK,
    AUTH_MODE_PORTAL,
    DEFAULT_ENDPOINTS,
    DEFAULT_PORTAL_SSO_URL,
    load_config,
    save_config,
)


# ── Config ────────────────────────────────────────────────────────────


def test_auth_mode_and_portal_url_round_trip_through_save_config(monkeypatch, tmp_path: Path):
    """save_config has dropped keys it did not know about before — both new
    settings must survive a Settings save."""
    env_path = tmp_path / "app.env"
    env_path.write_text(
        "AUTH_MODE=portal\nPORTAL_SSO_URL=https://portal.example/sso\n"
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()
    assert config.auth_mode == AUTH_MODE_PORTAL
    assert config.portal_sso_url == "https://portal.example/sso"

    save_config(config)
    reloaded = load_config()

    assert reloaded.auth_mode == AUTH_MODE_PORTAL
    assert reloaded.portal_sso_url == "https://portal.example/sso"


def test_auth_mode_defaults_to_mock(monkeypatch, tmp_path: Path):
    env_path = tmp_path / "app.env"
    env_path.write_text("API_BASE_URL=http://example.com\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    config = load_config()
    assert config.auth_mode == AUTH_MODE_MOCK
    assert config.portal_sso_url == DEFAULT_PORTAL_SSO_URL


def test_unknown_auth_mode_falls_back_to_mock(monkeypatch, tmp_path: Path):
    """A typo must not leave the operator staring at a half-configured screen."""
    env_path = tmp_path / "app.env"
    env_path.write_text("AUTH_MODE=Portall\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    assert load_config().auth_mode == AUTH_MODE_MOCK


def test_auth_mode_is_case_insensitive(monkeypatch, tmp_path: Path):
    env_path = tmp_path / "app.env"
    env_path.write_text("AUTH_MODE=PORTAL\n")
    monkeypatch.setenv("APP_CONFIG_PATH", str(env_path))

    assert load_config().auth_mode == AUTH_MODE_PORTAL


def test_logout_endpoint_is_configured():
    assert DEFAULT_ENDPOINTS["AUTH_LOGOUT"] == "/auth/logout"


# ── One-time code handling ────────────────────────────────────────────


def test_code_normalization_strips_every_kind_of_whitespace():
    """The portal shows the code in groups of 4; a paste brings newlines too."""
    assert normalize_one_time_code("abcd efgh\nijkl") == "abcdefghijkl"
    assert normalize_one_time_code("  abcd\tefgh \r\n") == "abcdefgh"
    assert normalize_one_time_code("abcdefgh") == "abcdefgh"
    assert normalize_one_time_code("   ") == ""


def test_sso_url_carries_the_device_id_url_encoded():
    from smart_gate.ui.login_view import build_sso_url

    url = build_sso_url("https://portal.example/sso", "dev id/1")
    assert url == "https://portal.example/sso?client=smart-gate&device_id=dev+id%2F1"

    # A trailing slash must not produce a double slash, and an existing query
    # string must be appended to, not overwritten.
    assert build_sso_url("https://portal.example/sso/", "d1").startswith(
        "https://portal.example/sso?"
    )
    assert "&client=smart-gate" in build_sso_url("https://portal.example/sso?a=b", "d1")


class _Repo:
    """Stand-in for DeviceRepository; each worker thread really has its own."""

    _MISSING = object()

    def __init__(self, refresh_token="refresh-1", device=_MISSING, events=None):
        self.device = (
            SimpleNamespace(device_id="dev-1", refresh_token=refresh_token)
            if device is self._MISSING
            else device
        )
        self.saved_refresh = None
        self.saved_profile = None
        self.cleared = False
        # Shared with the fake API in the logout tests, to assert ordering.
        self.events = events if events is not None else []

    def get_device(self):
        return self.device

    def update_refresh_token(self, device_id, token):
        self.saved_refresh = token
        if self.device is not None:
            self.device.refresh_token = token

    def save_user_profile(self, profile):
        self.saved_profile = profile

    def clear_session(self):
        self.cleared = True
        self.events.append("clear_session")


class _ExchangeApi:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.codes = []

    def desktop_exchange(self, code, device_id):
        self.codes.append((code, device_id))
        if self.error:
            raise self.error
        return self.response


def _exchange_response():
    return {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_in": 900,
        "user": {"uuid": "u-1", "email": "guard@sit.edu", "role": "guard"},
    }


def test_exchange_code_takes_the_same_path_as_a_mock_login():
    store = TokenStore()
    repo = _Repo(refresh_token=None)
    api = _ExchangeApi(_exchange_response())

    result = AuthService(api, repo, tokens=store).exchange_code("abcd efgh")

    assert api.codes == [("abcdefgh", "dev-1")]     # normalized before sending
    assert store.get_token() == "access-1"          # access token in memory only
    assert repo.saved_refresh == "refresh-1"        # refresh token persisted
    assert repo.saved_profile.email == "guard@sit.edu"
    assert result["user"]["uuid"] == "u-1"


def test_exchange_code_without_a_device_identity_fails_loudly():
    api = _ExchangeApi(_exchange_response())
    auth = AuthService(api, _Repo(device=None), tokens=TokenStore())

    with pytest.raises(RuntimeError):
        auth.exchange_code("abcd")
    assert api.codes == []


# ── LoginWorker error wording (portal mode only) ──────────────────────


def _login_worker_message(exc, code="abcd"):
    """Call the mapper unbound — constructing a QThread would need Qt running."""
    from smart_gate.main import LoginWorker

    return LoginWorker._error_message(SimpleNamespace(code=code), exc)


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code}", response=response)


def test_expired_code_reads_as_a_code_problem_not_a_server_problem():
    message = _login_worker_message(_http_error(401))
    assert "Invalid or expired code" in message
    assert "portal" in message.lower()


def test_unreachable_portal_has_its_own_message():
    message = _login_worker_message(requests.ConnectionError("no route to host"))
    assert message == (
        "Cannot reach the portal. Check the network connection and try again."
    )


def test_mock_mode_error_wording_is_unchanged():
    message = _login_worker_message(_http_error(401), code=None)
    assert message == "401"


# ── Device check: 404 from a portal that has no /devices/check ────────


class _CheckApi:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def check_device(self, token, device_id):
        if self.error:
            raise self.error
        return self.result


def _check(api):
    from smart_gate.main import LoginWorker

    return LoginWorker._check_device(api, "token", SimpleNamespace(device_id="dev-1"))


def test_missing_devices_check_endpoint_falls_back_to_offline_mode():
    """The portal has no /devices/check yet — a 404 must not block the gate."""
    result = _check(_CheckApi(error=_http_error(404)))
    assert result["registered"] is None      # unknown, not refused
    assert result["offline"] is True
    assert result["message"] == "device check not available — continuing in offline mode"


def test_not_implemented_devices_check_also_falls_back_to_offline_mode():
    result = _check(_CheckApi(error=_http_error(501)))
    assert result["offline"] is True
    assert "not available" in result["message"]


def test_server_error_on_device_check_is_offline_but_worded_as_a_failure():
    result = _check(_CheckApi(error=_http_error(500)))
    assert result["registered"] is None
    assert result["offline"] is True
    assert "500" in result["message"]


def test_connection_error_on_device_check_still_yields_offline_mode():
    result = _check(_CheckApi(error=requests.ConnectionError("down")))
    assert result["registered"] is None
    assert result["offline"] is True


def test_explicit_refusal_still_blocks_login():
    """Fail closed: only a *reachable* server saying no blocks the guard."""
    result = _check(_CheckApi({"registered": False, "message": "Device not registered."}))
    assert result["registered"] is False
    assert result["offline"] is False


# ── Refresh: rotation + single flight ─────────────────────────────────


class _RefreshApi:
    def __init__(self, responses=None, delay=0.0, config=None):
        self.responses = responses or []
        self.delay = delay
        self.calls = 0
        self.lock = threading.Lock()
        self.config = config

    def refresh(self, refresh_token):
        with self.lock:
            self.calls += 1
            index = min(self.calls - 1, len(self.responses) - 1)
        if self.delay:
            time.sleep(self.delay)
        return self.responses[index]


def test_rotated_refresh_token_is_persisted():
    repo = _Repo()
    api = _RefreshApi([{"access_token": "a2", "refresh_token": "refresh-2"}])

    assert AuthService(api, repo, tokens=TokenStore()).refresh_access_token() is True
    assert repo.saved_refresh == "refresh-2"


def test_response_without_rotation_leaves_the_stored_token_untouched():
    """The reference server does not rotate; nothing may be overwritten."""
    repo = _Repo()
    api = _RefreshApi([{"access_token": "a2"}])

    assert AuthService(api, repo, tokens=TokenStore()).refresh_access_token() is True
    assert repo.saved_refresh is None
    assert repo.device.refresh_token == "refresh-1"


def test_concurrent_refreshes_issue_exactly_one_http_call():
    """The bug this guards: under rotation, the second POST replays a consumed
    refresh token → 401 → the kiosk is thrown back to the login screen, and
    reuse detection may revoke the whole token family."""
    store = TokenStore()
    store.set_token("stale", expires_in=900)
    api = _RefreshApi(
        [{"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 900}],
        delay=0.15,
    )
    threads_count = 8
    barrier = threading.Barrier(threads_count)
    # Every thread reacts to the same observed staleness, exactly as
    # ensure_fresh_token/call_authed do before they touch the network.
    marker = refresh_coordinator.marker()
    seen_tokens = []
    seen_lock = threading.Lock()

    def worker():
        # Each thread has its own AuthService and its own repo/connection.
        auth = AuthService(api, _Repo(), tokens=store)
        barrier.wait()
        auth.refresh_access_token(seen_marker=marker)
        with seen_lock:
            seen_tokens.append(store.get_token())

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert api.calls == 1
    assert seen_tokens == ["fresh"] * threads_count


def test_concurrent_ensure_fresh_token_issues_exactly_one_http_call():
    store = TokenStore()
    store.set_token("stale", expires_in=900)
    store._issued_at = time.time() - 800   # 89% of the TTL burned
    api = _RefreshApi(
        [{"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 900}],
        delay=0.15,
    )
    threads_count = 6
    barrier = threading.Barrier(threads_count)
    results = []
    results_lock = threading.Lock()

    def worker():
        auth = AuthService(api, _Repo(), tokens=store)
        barrier.wait()
        token = auth.ensure_fresh_token(0.8)
        with results_lock:
            results.append(token)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert api.calls == 1
    assert results == ["fresh"] * threads_count


def test_a_401_for_a_token_another_thread_already_replaced_does_not_refresh_again():
    """The SyncWorker refreshed while this call was in flight: the 401 is stale
    news, so retry with the new token instead of burning a second refresh."""
    store = TokenStore()
    store.set_token("stale")
    api = _RefreshApi([{"access_token": "should-not-be-used"}])
    auth = AuthService(api, _Repo(), tokens=store)
    seen = []

    def call(token):
        seen.append(token)
        if token == "stale":
            # Simulate the other thread's refresh landing mid-call.
            store.set_token("fresh-from-other-thread")
            refresh_coordinator.bump()
            raise _http_error(401)
        return {"ok": True}

    assert auth.call_authed(call) == {"ok": True}
    assert api.calls == 0
    assert seen == ["stale", "fresh-from-other-thread"]


def test_a_genuine_401_after_the_last_refresh_still_refreshes():
    store = TokenStore()
    store.set_token("revoked")
    api = _RefreshApi([{"access_token": "fresh"}])
    auth = AuthService(api, _Repo(), tokens=store)

    def call(token):
        if token == "revoked":
            raise _http_error(401)
        return {"ok": True}

    assert auth.call_authed(call) == {"ok": True}
    assert api.calls == 1


# ── Logout ────────────────────────────────────────────────────────────


class _LogoutApi:
    def __init__(self, auth_mode=AUTH_MODE_PORTAL, error=None, events=None):
        self.config = SimpleNamespace(auth_mode=auth_mode)
        self.error = error
        self.events = events if events is not None else []
        self.revoked = []

    def logout(self, refresh_token, timeout=5):
        self.events.append("logout")
        self.revoked.append((refresh_token, timeout))
        if self.error:
            raise self.error


def test_portal_logout_revokes_server_side_before_clearing_local_state():
    """clear_session() deletes the very token the revoke needs — order matters."""
    events = []
    repo = _Repo(events=events)
    api = _LogoutApi(events=events)
    store = TokenStore()
    store.set_token("access-1")

    AuthService(api, repo, tokens=store).logout()

    assert events == ["logout", "clear_session"]
    assert api.revoked == [("refresh-1", 5)]
    assert store.get_token() is None
    assert repo.cleared is True


def test_a_failing_revoke_still_clears_the_local_session():
    """A gate with no network must still be able to log out."""
    repo = _Repo()
    api = _LogoutApi(error=requests.ConnectionError("no route to host"))
    store = TokenStore()
    store.set_token("access-1")

    AuthService(api, repo, tokens=store).logout()

    assert repo.cleared is True
    assert store.get_token() is None


def test_mock_mode_logout_never_calls_the_server():
    """The reference server has no /auth/logout — logout stays local."""
    repo = _Repo()
    api = _LogoutApi(auth_mode=AUTH_MODE_MOCK)
    store = TokenStore()
    store.set_token("access-1")

    AuthService(api, repo, tokens=store).logout()

    assert api.revoked == []
    assert repo.cleared is True


def test_logout_without_a_stored_refresh_token_skips_the_revoke():
    repo = _Repo(refresh_token=None)
    api = _LogoutApi()

    AuthService(api, repo, tokens=TokenStore()).logout()

    assert api.revoked == []
    assert repo.cleared is True
