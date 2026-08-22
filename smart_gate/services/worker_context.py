"""Per-thread service context.

``requests.Session`` and ``sqlite3.Connection`` are both bound to the thread
that created them; sharing one of each across the UI thread and three worker
threads is a data race waiting to happen.  Every worker therefore opens its own
connection and its own ``ApiClient`` for the duration of its run.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.services.auth_service import AuthService
from smart_gate.utils.config import AppConfig


@dataclass
class WorkerContext:
    conn: sqlite3.Connection
    api: ApiClient
    auth: AuthService
    device_repo: DeviceRepository
    allow_repo: AllowlistRepository


@contextmanager
def worker_context(config: AppConfig, db_path: Path) -> Iterator[WorkerContext]:
    """Open a thread-local DB connection + API client, closing both on exit."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    init_db(conn)
    try:
        device_repo = DeviceRepository(conn)
        api = ApiClient(config)
        yield WorkerContext(
            conn=conn,
            api=api,
            auth=AuthService(api, device_repo),
            device_repo=device_repo,
            allow_repo=AllowlistRepository(conn),
        )
    finally:
        conn.close()
