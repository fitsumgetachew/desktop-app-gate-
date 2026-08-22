from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional, TypeVar

import requests

from smart_gate.models.domain import UserProfile
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.services.token_store import TokenStore, token_store
from smart_gate.utils.config import AUTH_MODE_PORTAL

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SessionExpiredError(RuntimeError):
    """Raised when a 401 could not be recovered by refreshing the token."""


class TransientAuthError(RuntimeError):
    """A refresh attempt failed for a reason that says nothing about the token.

    Only the server rejecting the refresh token (401/403) means the session is
    over. A 429 from the portal's rate limiter, a 5xx, or a dropped connection
    all mean "unknown, ask again later" — treating those as a credential failure
    would sign a working gate out over a blip.
    """


def normalize_one_time_code(raw: str) -> str:
    """Strip every whitespace character from a portal one-time code.

    The portal renders the base64url code in groups of four, and an operator
    typing or pasting it brings the spaces — and, from a paste, newlines — along
    with it. base64url itself never contains whitespace, so dropping all of it is
    safe and spares the guard a "code invalid" they cannot see the cause of.
    """
    return "".join(str(raw).split())


class _RefreshCoordinator:
    """Process-wide single-flight guard around ``POST /auth/refresh``.

    Every worker thread builds its own :class:`AuthService` (see
    ``services/worker_context``) over its own SQLite connection, so a
    per-instance lock guards nothing: two threads happily read the *same*
    ``local_device_config.refresh_token`` and both post it.

    The portal rotates refresh tokens — the old one dies the instant the new one
    is issued — so the second request presents an already-consumed token, gets a
    401, and the gate is thrown back to the login screen. Worse, refresh-reuse is
    exactly what theft detection looks for, and it can revoke the whole token
    family, killing the thread that refreshed legitimately too.

    ``generation`` is bumped after every successful refresh. Callers snapshot it
    *before* they observe staleness (a nearing expiry, or a 401 from a request
    they just made); if it has moved by the time they hold the lock, another
    thread already fixed the thing they were reacting to and they reuse its
    token instead of burning a second one.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        # Plain int read/write: atomic under the GIL, so ``marker()`` never has
        # to block on a refresh that is currently in flight.
        self._generation = 0

    def marker(self) -> int:
        return self._generation

    def bump(self) -> None:
        self._generation += 1


# One coordinator per process, shared by every AuthService instance.
refresh_coordinator = _RefreshCoordinator()


class AuthService:
    """Owns the login flow and the access-token lifecycle.

    Every authenticated call should go through :meth:`call_authed` so a 401 is
    transparently retried once with a freshly refreshed token instead of
    surfacing as a random failure in whichever worker happened to make it.
    """

    def __init__(
        self,
        api: ApiClient,
        device_repo: DeviceRepository,
        tokens: Optional[TokenStore] = None,
    ) -> None:
        self.api = api
        self.device_repo = device_repo
        self.tokens = tokens or token_store

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> Dict[str, Any]:
        """Mock-mode sign-in: the desktop drives both steps itself.

          1. POST /auth/desktop/start  → one-time code
          2. POST /auth/desktop/exchange → access_token + refresh_token + user

        Returns dict with keys: access_token, refresh_token, user
        """
        device_id = self._require_device_id()
        # Step 1 — get one-time code
        start_resp = self.api.desktop_start(device_id, email, password)
        # Step 2 — exchange code for tokens
        return self.exchange_code(start_resp["code"])

    def exchange_code(self, code: str) -> Dict[str, Any]:
        """Step 2 on its own — the only step the desktop runs in portal mode.

        In portal mode the operator authenticates in a browser and the portal
        mints ``code``; no credential ever reaches this process. Both modes end
        up here so the token/profile persistence below is literally the same
        code path.
        """
        device_id = self._require_device_id()
        exchange_resp = self.api.desktop_exchange(normalize_one_time_code(code), device_id)
        return self._persist_session(device_id, exchange_resp)

    def _require_device_id(self) -> str:
        device = self.device_repo.get_device()
        if not device:
            raise RuntimeError("Device not initialised. Cannot log in.")
        return device.device_id

    def _persist_session(self, device_id: str, exchange_resp: Dict[str, Any]) -> Dict[str, Any]:
        """Store the tokens and user profile from a successful exchange."""
        access_token = exchange_resp["access_token"]
        refresh_token = exchange_resp["refresh_token"]
        user = exchange_resp["user"]

        # Access token stays in memory; only the refresh token is persisted.
        self.tokens.set_token(access_token, exchange_resp.get("expires_in"))
        self.device_repo.update_refresh_token(device_id, refresh_token)

        # Persist user profile (uuid field from new API)
        profile = UserProfile(
            uuid=user["uuid"],
            email=user["email"],
            full_name=user.get("full_name", ""),
            role=user.get("role", ""),
        )
        self.device_repo.save_user_profile(profile)

        return {"access_token": access_token, "refresh_token": refresh_token, "user": user}

    # ------------------------------------------------------------------
    # Token lifecycle
    # ------------------------------------------------------------------

    def refresh_access_token(self, seen_marker: Optional[int] = None) -> bool:
        """Swap the stored refresh token for a new access token — single-flight.

        Only one ``/auth/refresh`` may be in flight per process; threads that
        arrive while it runs reuse its result rather than replaying a refresh
        token the server has already rotated away. ``seen_marker`` is the
        coordinator marker taken *before* the caller decided a refresh was
        needed — see :class:`_RefreshCoordinator`.

        Returns True on success. The new access token goes to the in-memory
        store; a rotated refresh token (if the server sends one) is persisted.
        """
        if seen_marker is None:
            seen_marker = refresh_coordinator.marker()

        with refresh_coordinator.lock:
            if refresh_coordinator.marker() != seen_marker:
                # Another thread refreshed while we waited for the lock — its
                # token is already in the store and ours is dead.
                logger.debug("Refresh already performed by another thread — reusing it")
                return self.tokens.has_token()

            # The read-modify-write of local_device_config.refresh_token stays
            # inside the lock: every thread has its own SQLite connection, so
            # interleaving here would resurrect a consumed token.
            device = self.device_repo.get_device()
            if not device or not device.refresh_token:
                logger.warning("No refresh token available")
                return False
            try:
                resp = self.api.refresh(device.refresh_token)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    logger.warning("Refresh token rejected by the server (HTTP %s)", status)
                    return False
                # 429 (rate limited), 5xx, anything else: the token may well be
                # fine. Keep the session and let the caller retry.
                logger.warning("Token refresh failed transiently (HTTP %s)", status)
                raise TransientAuthError(f"refresh failed with HTTP {status}") from exc
            except requests.RequestException as exc:
                logger.warning("Token refresh unreachable: %s", exc)
                raise TransientAuthError("refresh could not reach the server") from exc
            except Exception as exc:  # unexpected: a bug, a malformed response
                # Ending the session is the destructive option, and a surprise
                # here is no evidence the credentials are bad. Keep them.
                logger.warning("Token refresh raised unexpectedly: %s", exc)
                raise TransientAuthError("refresh failed unexpectedly") from exc

            access_token = resp.get("access_token")
            if not access_token:
                logger.warning("Refresh response contained no access_token")
                return False

            self.tokens.set_token(access_token, resp.get("expires_in"))
            rotated = resp.get("refresh_token")
            if rotated and rotated != device.refresh_token:
                self.device_repo.update_refresh_token(device.device_id, rotated)
            refresh_coordinator.bump()
            logger.info("Access token refreshed successfully")
            return True

    def ensure_fresh_token(self, ratio: float = 0.8) -> Optional[str]:
        """Refresh proactively once ``ratio`` of the token's TTL has elapsed.

        Reacting only to 401s means every expiry costs one failed round-trip;
        refreshing at ~80% of the 900 s TTL keeps the token valid ahead of time.
        """
        # Snapshot before the staleness check, not after: if another thread
        # refreshes in between, the marker has moved and we skip our own.
        marker = refresh_coordinator.marker()
        if self.tokens.needs_refresh(ratio):
            logger.info("Access token nearing expiry — refreshing proactively")
            try:
                self.refresh_access_token(seen_marker=marker)
            except TransientAuthError as exc:
                # The current token is still valid for the remaining 20% of its
                # TTL; carry on with it and try again next cycle.
                logger.info("Proactive refresh deferred: %s", exc)
        return self.tokens.get_token()

    def call_authed(self, fn: Callable[[str], T]) -> T:
        """Run ``fn(token)``, refreshing and retrying once on a 401.

        Raises :class:`SessionExpiredError` when the refresh itself fails, so
        callers can route the user back to the login screen.
        """
        # Snapshot before the call: a 401 for a token that another thread has
        # already replaced needs that thread's token, not a second refresh.
        marker = refresh_coordinator.marker()
        token = self.tokens.get_token() or ""
        try:
            return fn(token)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 401:
                raise
            logger.info("Got 401 — refreshing access token and retrying once")
            if not self.refresh_access_token(seen_marker=marker):
                raise SessionExpiredError("Session expired — please sign in again.") from exc
            return fn(self.tokens.get_token() or "")

    # ------------------------------------------------------------------
    # Sign-out
    # ------------------------------------------------------------------

    def logout(self) -> None:
        """Drop the local session, revoking it server-side first in portal mode.

        The revoke is best-effort: a gate with no network must still be able to
        log out, so failures are logged and swallowed.
        """
        if self._auth_mode() == AUTH_MODE_PORTAL:
            self._revoke_session()
        self.tokens.clear()
        self.device_repo.clear_session()

    def _revoke_session(self) -> None:
        """POST /auth/logout before clear_session() deletes the token it needs."""
        device = self.device_repo.get_device()
        refresh_token = getattr(device, "refresh_token", None) if device else None
        if not refresh_token:
            return
        try:
            self.api.logout(refresh_token)
            logger.info("Session revoked server-side")
        except Exception as exc:
            logger.warning("Server-side logout failed (ignored): %s", exc)

    def _auth_mode(self) -> str:
        return getattr(getattr(self.api, "config", None), "auth_mode", "mock")
