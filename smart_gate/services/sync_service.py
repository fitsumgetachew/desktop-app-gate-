from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import requests
from PySide6 import QtCore

from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.repositories.event_repo import EventRepository
from smart_gate.repositories.manual_reason_repo import ManualReasonRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)


class SyncWorker(QtCore.QThread):
    online_changed = QtCore.Signal(bool)
    sync_status = QtCore.Signal(str)
    last_sync_time = QtCore.Signal(int)
    next_sync_time = QtCore.Signal(int)
    sync_running = QtCore.Signal(bool)

    def __init__(
        self,
        api: ApiClient,
        db_path: Path,
        interval_seconds: int,
    ) -> None:
        super().__init__()
        self.api = api
        self.db_path = db_path
        self._interval_seconds = max(interval_seconds, 5)
        self._stop_flag = False
        self._online = False
        self._backoff = 2
        self._next_wait_seconds = self._interval_seconds
        self._trigger_sync = True
        self._mutex = QtCore.QMutex()
        self._wait = QtCore.QWaitCondition()

        self._conn: sqlite3.Connection | None = None
        self.device_repo: DeviceRepository | None = None
        self.allow_repo: AllowlistRepository | None = None
        self.reason_repo: ManualReasonRepository | None = None
        self.event_repo: EventRepository | None = None

    def stop(self) -> None:
        self._mutex.lock()
        self._stop_flag = True
        self._wait.wakeAll()
        self._mutex.unlock()

    def set_interval(self, interval_seconds: int) -> None:
        self._mutex.lock()
        self._interval_seconds = max(interval_seconds, 5)
        self._next_wait_seconds = self._interval_seconds
        next_time = now_ts() + self._next_wait_seconds
        self.next_sync_time.emit(next_time)
        self._wait.wakeAll()
        self._mutex.unlock()

    def trigger_sync(self) -> None:
        self._mutex.lock()
        self._trigger_sync = True
        self._wait.wakeAll()
        self._mutex.unlock()

    def run(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        init_db(self._conn)
        self.device_repo = DeviceRepository(self._conn)
        self.allow_repo = AllowlistRepository(self._conn)
        self.reason_repo = ManualReasonRepository(self._conn)
        self.event_repo = EventRepository(self._conn)

        while True:
            self._mutex.lock()
            if self._stop_flag:
                self._mutex.unlock()
                break
            if not self._trigger_sync:
                next_time = now_ts() + self._next_wait_seconds
                self.next_sync_time.emit(next_time)
                self._wait.wait(self._mutex, int(self._next_wait_seconds * 1000))
                if self._stop_flag:
                    self._mutex.unlock()
                    break
            self._trigger_sync = False
            self._mutex.unlock()

            self.sync_running.emit(True)
            self.sync_status.emit("Sync started...")
            attempted = False
            try:
                attempted = self._sync_once()
                if attempted:
                    self._set_online(True)
                    self._backoff = 2
                    self._next_wait_seconds = self._interval_seconds
                    self.sync_status.emit("Sync success")
                    self.last_sync_time.emit(now_ts())
                else:
                    self._set_online(False)
                    self.sync_status.emit("Not logged in")
            except requests.RequestException as exc:
                logger.warning("Sync request failed: %s", exc)
                self._set_online(False)
                self.sync_status.emit("Sync failed (offline)")
                self._backoff = min(self._backoff * 2, 60)
                self._next_wait_seconds = self._backoff
                if attempted:
                    self.last_sync_time.emit(now_ts())
            except Exception:
                logger.exception("Unexpected sync error")
                self._set_online(False)
                self.sync_status.emit("Sync failed")
                self._backoff = min(self._backoff * 2, 60)
                self._next_wait_seconds = self._backoff
                if attempted:
                    self.last_sync_time.emit(now_ts())
            finally:
                next_time = now_ts() + self._next_wait_seconds
                self.next_sync_time.emit(next_time)
                self.sync_running.emit(False)

        if self._conn:
            self._conn.close()

    def _set_online(self, online: bool) -> None:
        if self._online != online:
            self._online = online
            self.online_changed.emit(online)

    def _sync_once(self) -> bool:
        if not self.device_repo or not self.allow_repo or not self.reason_repo or not self.event_repo:
            return False
        device = self.device_repo.get_device()
        if not device or not device.access_token:
            return False
        token = device.access_token

        since_version = self.allow_repo.get_last_version()
        allow_resp = self.api.get_allowlist(token, since_version)
        version = int(allow_resp.get("version", now_ts()))
        items = allow_resp.get("items", [])
        allow_items = []
        for item in items:
            allow_items.append(
                (
                    item["plate_number"],
                    item["status"],
                    item.get("valid_to"),
                    int(item["updated_at"]),
                    version,
                )
            )
        if allow_items:
            self.allow_repo.upsert_items(allow_items)

        try:
            reason_resp = self.api.get_manual_reasons(token)
            reasons = reason_resp.get("items", [])
            reason_items = []
            for reason in reasons:
                reason_items.append(
                    (
                        int(reason["id"]),
                        reason["reason_text"],
                        1 if reason.get("is_active", True) else 0,
                        now_ts(),
                    )
                )
            if reason_items:
                self.reason_repo.replace_all(reason_items)
        except requests.RequestException:
            logger.info("Manual reasons sync failed; skipping")

        unsynced = self.event_repo.list_unsynced()
        for row in unsynced:
            payload = {
                "event_time": row["event_time"],
                "device_id": row["device_id"],
                "gate_id": row["gate_id"],
                "lane_id": row["lane_id"],
                "direction": row["direction"],
                "plate_number_raw": row["plate_number_raw"],
                "plate_number_final": row["plate_number_final"],
                "confidence": row["confidence"],
                "decision": row["decision"],
                "decision_source": row["decision_source"],
                "manual_by": row["manual_by_username"],
                "manual_reason": row["manual_reason"],
                "manual_note": row["manual_note"],
                "is_offline_event": bool(row["is_offline_event"]),
                "evidence_local_path": row["evidence_path"],
                "evidence_uploaded_url": None,
            }
            try:
                self.api.post_event(token, payload)
                self.event_repo.mark_synced(row["id"])
            except requests.RequestException as exc:
                self.event_repo.increment_sync_attempt(row["id"], str(exc))
                raise
        return True
