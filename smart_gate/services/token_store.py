"""Process-wide, in-memory holder for the access token.

The access token is deliberately **never** written to SQLite: it is a bearer
credential with a 900 s lifetime and the local database is world-readable on a
shared gate PC.  Worker threads all live in the same process, so a single
thread-safe module-level store is enough to share it.

TODO(security): the *refresh* token is still persisted in
``local_device_config.refresh_token``.  It must move to the OS keyring
(``keyring``/libsecret on Linux, DPAPI on Windows, Keychain on macOS) before
production deployment.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

DEFAULT_TTL_SECONDS = 900
REFRESH_AT_RATIO = 0.8  # refresh once 80% of the TTL has elapsed


class TokenStore:
    """Thread-safe access-token holder with TTL bookkeeping."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token: Optional[str] = None
        self._issued_at: float = 0.0
        self._ttl: int = DEFAULT_TTL_SECONDS

    def set_token(self, token: str, expires_in: Optional[int] = None) -> None:
        with self._lock:
            self._token = token
            self._issued_at = time.time()
            self._ttl = int(expires_in) if expires_in else DEFAULT_TTL_SECONDS

    def get_token(self) -> Optional[str]:
        with self._lock:
            return self._token

    def has_token(self) -> bool:
        return self.get_token() is not None

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._issued_at = 0.0
            self._ttl = DEFAULT_TTL_SECONDS

    def age_seconds(self) -> float:
        with self._lock:
            if not self._token:
                return 0.0
            return time.time() - self._issued_at

    def needs_refresh(self, ratio: float = REFRESH_AT_RATIO) -> bool:
        """True once the token has burned through ``ratio`` of its lifetime."""
        with self._lock:
            if not self._token:
                return False
            return (time.time() - self._issued_at) >= (self._ttl * ratio)


# Single instance shared by the UI thread and every worker thread.
token_store = TokenStore()
