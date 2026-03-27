from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


import requests
from PySide6 import QtCore

from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.repositories.event_repo import EventRepository
from smart_gate.repositories.manual_reason_repo import ManualReasonRepository
from smart_gate.repositories.presence_repo import PresenceRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)

APP_VERSION = "2.0.0"


class SyncWorker(QtCore.QThread):
    online_changed = QtCore.Signal(bool)
    sync_status = QtCore.Signal(str)
    last_sync_time = QtCore.Signal(int)
    next_sync_time = QtCore.Signal(int)
    sync_running = QtCore.Signal(bool)
    auth_required = QtCore.Signal()   # emitted when token refresh fails → re-login needed

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
        self._heartbeat_counter = 0   # send heartbeat every N sync cycles

        self._conn: sqlite3.Connection | None = None
        self.device_repo: DeviceRepository | None = None
        self.allow_repo: AllowlistRepository | None = None
        self.reason_repo: ManualReasonRepository | None = None
        self.event_repo: EventRepository | None = None
        self.presence_repo: PresenceRepository | None = None

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
        self.presence_repo = PresenceRepository(self._conn)

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
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    logger.warning("Received 401 during sync, attempting token refresh")
                    if self._try_refresh_token():
                        # Token refreshed; next cycle will retry with the new token.
                        self._set_online(True)
                        self.sync_status.emit("Token refreshed — retrying next cycle")
                        self._next_wait_seconds = 2  # retry soon
                    else:
                        logger.warning("Token refresh failed — re-login required")
                        self._set_online(False)
                        self.sync_status.emit("Session expired")
                        self.auth_required.emit()
                        # Back off then stop protecting; let UI handle re-login
                        self._next_wait_seconds = self._interval_seconds
                else:
                    logger.warning("Sync HTTP error: %s", exc)
                    self._set_online(False)
                    status_code = exc.response.status_code if exc.response is not None else "?"
                    self.sync_status.emit(f"Sync failed (HTTP {status_code})")
                    self._backoff = min(self._backoff * 2, 60)
                    self._next_wait_seconds = self._backoff
                    if attempted:
                        self.last_sync_time.emit(now_ts())
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

    def _try_refresh_token(self) -> bool:
        """Attempt to refresh the access token using the stored refresh token.
        Returns True if successful, False otherwise."""
        if not self.device_repo:
            return False
        device = self.device_repo.get_device()
        if not device or not device.refresh_token:
            logger.warning("No refresh token available")
            return False
        try:
            resp = self.api.refresh(device.refresh_token)
            new_token = resp["access_token"]
            self.device_repo.update_access_token(device.device_id, new_token)
            logger.info("Access token refreshed successfully")
            return True
        except Exception as exc:
            logger.warning("Token refresh failed: %s", exc)
            return False

    def _get_token(self) -> Optional[str]:
        if not self.device_repo:
            return None
        device = self.device_repo.get_device()
        return device.access_token if device else None

    def _sync_once(self) -> bool:
        """Run one full sync cycle. Returns True if authenticated and attempted."""
        if not all([self.device_repo, self.allow_repo, self.reason_repo,
                    self.event_repo, self.presence_repo]):
            return False
        device = self.device_repo.get_device()
        if not device or not device.access_token:
            return False
        token = device.access_token

        # ── Pull allowlist (delta sync) ──────────────────────────────
        since_version = self.allow_repo.get_last_version()
        allow_resp = self.api.get_allowlist(token, since_version)
        version = int(allow_resp.get("version", now_ts()))
        items = allow_resp.get("items", [])
        allow_items = []
        for item in items:
            allow_items.append((
                item["plate_number"],
                item["status"],
                item.get("valid_to"),
                item.get("owner_name"),
                int(item["updated_at"]),
                version,
            ))
        if allow_items:
            self.allow_repo.upsert_items(allow_items)

        # ── Pull manual reasons (soft-fail) ──────────────────────────
        try:
            reason_resp = self.api.get_manual_reasons(token)
            reasons = reason_resp.get("items", [])
            reason_items = [
                (
                    int(r["id"]),
                    r["reason_text"],
                    1 if r.get("is_active", True) else 0,
                    now_ts(),
                )
                for r in reasons
            ]
            if reason_items:
                self.reason_repo.replace_all(reason_items)
        except requests.RequestException:
            logger.info("Manual reasons sync failed; skipping")

        # ── Push unsynced events via batch ───────────────────────────
        unsynced = self.event_repo.list_unsynced()
        if unsynced:
            self._push_events_batch(token, unsynced)

        # ── Upload pending evidence files (soft-fail) ────────────────
        try:
            self._upload_pending_evidence(token)
        except Exception:
            logger.exception("Evidence upload phase failed unexpectedly")

        # ── Heartbeat (every 5 sync cycles) ─────────────────────────
        self._heartbeat_counter += 1
        if self._heartbeat_counter >= 5:
            self._heartbeat_counter = 0
            self._send_heartbeat(token, device.device_id)

        return True

    def _build_event_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Build the API payload for a single event row."""
        return {
            "id": row["id"],                              # idempotency key
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
            "manual_by_user_id": row["manual_by_user_id"] if "manual_by_user_id" in row.keys() else None,
            "manual_by_username": row["manual_by_username"],
            "manual_reason_id": row["manual_reason_id"] if "manual_reason_id" in row.keys() else None,
            "manual_reason_text": row["manual_reason"],
            "manual_note": row["manual_note"],
            "is_offline_event": bool(row["is_offline_event"]),
            "evidence_uploaded_url": None,  # local evidence path is never sent to server
        }

    def _push_events_batch(self, token: str, unsynced: List[sqlite3.Row]) -> None:
        """
        Submit the batch. Per-item failures do NOT abort the batch — each event is
        processed independently and marked accordingly.
        """
        items = [self._build_event_payload(row) for row in unsynced]
        batch_resp = self.api.post_events_batch(token, items)
        results: List[Dict[str, Any]] = batch_resp.get("results", [])

        # Index results by event_id for O(1) lookup
        results_by_id = {r["event_id"]: r for r in results}

        for row in unsynced:
            result = results_by_id.get(row["id"])
            if result is None:
                # Server didn't include this event in response — leave for next cycle
                continue

            if result.get("ok"):
                self.event_repo.mark_synced(row["id"])
                # Update local presence hint from server-authoritative state
                pu = result.get("presence_update")
                if pu and pu.get("plate_number"):
                    state = pu.get("inside_status", "UNKNOWN")
                    self.presence_repo.upsert_presence(
                        pu["plate_number"], state, now_ts()
                    )
            else:
                error_msg = result.get("error") or "Server rejected event"
                logger.warning("Event %s rejected by server: %s", row["id"], error_msg)
                self.event_repo.increment_sync_attempt(row["id"], error_msg)

    def _upload_pending_evidence(self, token: str) -> None:
        pending = self.event_repo.list_pending_evidence_upload()
        for row in pending:
            event_id = row["id"]
            evidence_path = row["evidence_path"]
            if not evidence_path or not Path(evidence_path).exists():
                self.event_repo.increment_evidence_upload_attempt(event_id, "File not found on disk")
                continue
            try:
                url_resp = self.api.get_evidence_upload_url(token, event_id)
                upload_url = url_resp["upload_url"]
                upload_method = url_resp.get("upload_method", "multipart")
                upload_resp = self.api.upload_evidence(upload_url, evidence_path, upload_method)
                file_url = upload_resp.get("file_url", "") if upload_resp else ""
                self.event_repo.mark_evidence_uploaded(event_id, file_url)
                logger.info("Evidence uploaded for event %s → %s", event_id, file_url)
            except Exception as exc:
                logger.warning("Evidence upload failed for event %s: %s", event_id, exc)
                self.event_repo.increment_evidence_upload_attempt(event_id, str(exc))

    def _send_heartbeat(self, token: str, device_id: str) -> None:
        try:
            payload = {
                "device_id": device_id,
                "app_version": APP_VERSION,
                "status": "OK",
                "last_error": None,
            }
            self.api.heartbeat(token, payload)
            logger.debug("Heartbeat sent")
        except Exception as exc:
            logger.debug("Heartbeat failed (non-critical): %s", exc)
