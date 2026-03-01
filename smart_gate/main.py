from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path

import cv2
from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.models.domain import EventRecord
from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import Database, init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.repositories.event_repo import EventRepository
from smart_gate.repositories.manual_reason_repo import ManualReasonRepository
from smart_gate.repositories.presence_repo import PresenceRepository
from smart_gate.services.api_client import ApiClient
from smart_gate.services.auth_service import AuthService
from smart_gate.services.camera_service import CameraService
from smart_gate.services.device_service import DeviceService
from smart_gate.services.sync_service import SyncWorker
from smart_gate.ui.login_view import LoginView
from smart_gate.ui.main_view import MainGateView
from smart_gate.ui.settings_view import SettingsPage
from smart_gate.utils.config import AppConfig, load_config
from smart_gate.ui.theme import SIT_STYLESHEET
from smart_gate.utils.logging import setup_logging
from smart_gate.utils.paths import ensure_dir
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)


class LoginWorker(QtCore.QThread):
    login_success = QtCore.Signal(str)
    login_error = QtCore.Signal(str)

    def __init__(self, config: AppConfig, db_path: Path, email: str, password: str) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.email = email
        self.password = password

    def run(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            init_db(conn)

            device_repo = DeviceRepository(conn)
            api = ApiClient(self.config)
            auth_service = AuthService(api, device_repo)
            data = auth_service.login(self.email, self.password)
            self.login_success.emit(data["access_token"])
        except Exception as exc:
            logger.exception("Login failed")
            self.login_error.emit(str(exc))
        finally:
            if conn:
                conn.close()


class EventUploadWorker(QtCore.QThread):
    upload_success = QtCore.Signal(str)
    upload_error = QtCore.Signal(str, str)

    def __init__(self, api: ApiClient, token: str, event_id: str, payload: dict) -> None:
        super().__init__()
        self.api = api
        self.token = token
        self.event_id = event_id
        self.payload = payload

    def run(self) -> None:
        try:
            self.api.post_event(self.token, self.payload)
            self.upload_success.emit(self.event_id)
        except Exception as exc:
            self.upload_error.emit(self.event_id, str(exc))


class LookupWorker(QtCore.QThread):
    lookup_success = QtCore.Signal(dict)
    lookup_not_found = QtCore.Signal()
    lookup_error = QtCore.Signal(str)

    def __init__(self, api: ApiClient, token: str, plate_number: str) -> None:
        super().__init__()
        self.api = api
        self.token = token
        self.plate_number = plate_number

    def run(self) -> None:
        try:
            data = self.api.lookup_vehicle(self.token, self.plate_number)
            self.lookup_success.emit(data)
        except Exception as exc:
            message = str(exc)
            if "404" in message or "Not Found" in message:
                self.lookup_not_found.emit()
            else:
                self.lookup_error.emit(message)


class RegisterVehicleWorker(QtCore.QThread):
    register_success = QtCore.Signal(dict)
    register_error = QtCore.Signal(str)

    def __init__(self, api: ApiClient, token: str, payload: dict) -> None:
        super().__init__()
        self.api = api
        self.token = token
        self.payload = payload

    def run(self) -> None:
        try:
            data = self.api.register_vehicle(self.token, self.payload)
            self.register_success.emit(data)
        except Exception as exc:
            self.register_error.emit(str(exc))


class AppWindow(QtWidgets.QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.setWindowTitle("Smart Gate Desktop")

        # Use the default Qt.Window flag – this gives a native OS title bar
        # with close / minimise / maximise buttons and resizable edges on
        # every platform.  Do NOT add explicit button-hint flags here
        # because on Linux / Ubuntu that *removes* the system menu hint
        # and can break the title-bar behaviour.
        self.setWindowFlags(QtCore.Qt.Window)
        self.setMinimumSize(900, 560)

        self.config = config

        self.db = Database()
        self.conn = self.db.connect()
        init_db(self.conn)

        self.device_repo = DeviceRepository(self.conn)
        self.allow_repo = AllowlistRepository(self.conn)
        self.reason_repo = ManualReasonRepository(self.conn)
        self.event_repo = EventRepository(self.conn)
        self.presence_repo = PresenceRepository(self.conn)

        self.api = ApiClient(config)
        self.auth_service = AuthService(self.api, self.device_repo)
        self.device_service = DeviceService(self.api, self.device_repo)

        self.device = self.device_service.ensure_device(
            gate_id=config.gate_id,
            lane_id=config.lane_id,
            device_name=config.device_name,
        )

        self.camera_service = CameraService(
            mode=config.camera_mode,
            index=config.camera_index,
            rtsp_url=config.camera_rtsp_url,
        )

        self.sync_worker = self._create_sync_worker()

        self.is_online = False
        self.last_capture_path: str | None = None
        self._uploaders: list[EventUploadWorker] = []
        self._pending_recheck_plate: str | None = None

        self.stack = QtWidgets.QStackedWidget()
        self.login_view = LoginView()
        self.main_view = MainGateView()
        self.settings_page = SettingsPage()

        self.stack.addWidget(self.login_view)
        self.stack.addWidget(self.main_view)
        self.stack.addWidget(self.settings_page)

        self.setCentralWidget(self.stack)

        self._connect_signals()
        self._refresh_recent_events()
        self._refresh_manual_reasons()
        self.main_view.set_gate_lane(self.config.gate_id, self.config.lane_id)
        self.main_view.set_user("-")

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self._refresh_recent_events)
        self.refresh_timer.timeout.connect(self._refresh_manual_reasons)
        self.refresh_timer.start()

        self.next_sync_at: int | None = None
        self.sync_countdown_timer = QtCore.QTimer(self)
        self.sync_countdown_timer.setInterval(1000)
        self.sync_countdown_timer.timeout.connect(self._update_next_sync_label)
        self.sync_countdown_timer.start()

    def _connect_signals(self) -> None:
        self.login_view.login_requested.connect(self._handle_login)
        self.camera_service.frame_ready.connect(self.main_view.update_frame)
        self.camera_service.status_changed.connect(self.main_view.set_camera_status)
        self.main_view.decision_requested.connect(self._handle_decision)
        self.main_view.capture_requested.connect(self._handle_capture)
        self.main_view.settings_requested.connect(self._open_settings)
        self.main_view.logout_requested.connect(self._handle_logout)
        self.main_view.sync_now_requested.connect(self._handle_sync_now)
        self.main_view.check_status_requested.connect(self._handle_check_status)
        self.main_view.sync_recheck_requested.connect(self._handle_sync_recheck)
        self.main_view.add_temp_permit_requested.connect(self._handle_add_temp_permit)
        self.main_view.fullscreen_requested.connect(self._toggle_fullscreen)
        self.settings_page.settings_saved.connect(self._on_settings_saved)
        self.settings_page.settings_cancelled.connect(self._handle_settings_cancelled)
        self._connect_sync_signals()

    def _connect_sync_signals(self) -> None:
        self.sync_worker.online_changed.connect(self._handle_online)
        self.sync_worker.sync_status.connect(self._handle_sync_status)
        self.sync_worker.last_sync_time.connect(self._handle_last_sync_time)
        self.sync_worker.next_sync_time.connect(self._handle_next_sync_time)

    def _create_sync_worker(self) -> SyncWorker:
        worker = SyncWorker(
            api=self.api,
            db_path=self.db.db_path,
            interval_seconds=self.config.sync_interval_seconds,
        )
        return worker

    def _handle_login(self, email: str, password: str) -> None:
        self.login_view.set_status("Logging in...")
        self.login_worker = LoginWorker(self.config, self.db.db_path, email, password)
        self.login_worker.login_success.connect(self._on_login_success)
        self.login_worker.login_error.connect(self._on_login_error)
        self.login_worker.start()

    def _on_login_success(self, token: str) -> None:
        self.login_view.set_status("Login success")
        try:
            device = self.device_repo.get_device()
            if device:
                self.device_service.register_device(token, device)
        except Exception as exc:
            logger.warning("Device registration failed: %s", exc)
        user_profile = self.device_repo.get_user_profile()
        if user_profile:
            self.main_view.set_user(user_profile.email)
        self.main_view.set_gate_lane(self.config.gate_id, self.config.lane_id)
        self.stack.setCurrentWidget(self.main_view)
        self.camera_service.start()
        if not self.sync_worker.isRunning():
            self.sync_worker.start()
        else:
            self.sync_worker.trigger_sync()
        self._refresh_manual_reasons()
        self._refresh_recent_events()

    def _on_login_error(self, message: str) -> None:
        self.login_view.set_status(f"Login failed: {message}")

    def _handle_online(self, online: bool) -> None:
        self.is_online = online
        self.main_view.set_online_status(online)

    def _handle_sync_status(self, message: str) -> None:
        self.main_view.set_sync_status(message)

    def _handle_last_sync_time(self, ts: int) -> None:
        self.main_view.set_last_sync(self._format_ts(ts))
        if self._pending_recheck_plate:
            self.main_view.set_plate_text(self._pending_recheck_plate)
            self._pending_recheck_plate = None
            self._handle_check_status()

    def _handle_next_sync_time(self, ts: int) -> None:
        self.next_sync_at = ts
        self._update_next_sync_label()

    def _refresh_manual_reasons(self) -> None:
        reasons = self.reason_repo.list_active()
        if not reasons:
            reasons = ["Manual override"]
        self.main_view.set_reasons(reasons)

    def _refresh_recent_events(self) -> None:
        rows = self.event_repo.list_recent()
        self.main_view.set_recent_events(rows)

    def _handle_capture(self) -> None:
        frame = self.camera_service.capture_current_frame()
        if frame is None:
            QtWidgets.QMessageBox.warning(self, "Capture", "No camera frame available")
            return
        path = self._save_frame(frame, suffix="capture")
        self.last_capture_path = path
        QtWidgets.QMessageBox.information(self, "Capture", f"Saved to {path}")

    def _save_frame(self, frame, suffix: str) -> str:
        evidence_dir = ensure_dir(Path(self.config.evidence_dir))
        filename = f"{now_ts()}_{suffix}.jpg"
        full_path = evidence_dir / filename
        cv2.imwrite(str(full_path), frame)
        return str(full_path)

    def _handle_decision(self, decision: str) -> None:
        plate_raw, reason, note = self.main_view.get_manual_inputs()
        plate = self._normalize_plate(plate_raw)
        if not plate:
            QtWidgets.QMessageBox.warning(self, "Decision", "Plate number is required")
            return
        self.main_view.set_plate_text(plate)

        frame = self.camera_service.capture_current_frame()
        evidence_path = None
        if frame is not None:
            evidence_path = self._save_frame(frame, suffix=plate.replace(" ", ""))

        user_profile = self.device_repo.get_user_profile()
        manual_by = user_profile.email if user_profile else None

        event_id = str(uuid.uuid4())
        ts = now_ts()
        event = EventRecord(
            id=event_id,
            event_time=ts,
            gate_id=self.config.gate_id,
            lane_id=self.config.lane_id,
            device_id=self.device.device_id,
            direction=self.config.direction,
            plate_number_raw=plate_raw,
            plate_number_final=plate,
            confidence=None,
            decision=decision,
            decision_source="MANUAL",
            manual_by_username=manual_by,
            manual_reason=reason,
            manual_note=note,
            is_offline_event=not self.is_online,
            evidence_path=evidence_path,
            synced=False,
            sync_attempts=0,
            last_sync_error=None,
            created_at=ts,
        )
        self.event_repo.add_event(event)
        if decision == "ALLOW":
            self._update_presence_hint(event.plate_number_final, event.direction)
        self._refresh_recent_events()

        device = self.device_repo.get_device()
        if device and device.access_token and self.is_online:
            payload = {
                "event_time": event.event_time,
                "device_id": event.device_id,
                "gate_id": event.gate_id,
                "lane_id": event.lane_id,
                "direction": event.direction,
                "plate_number_raw": event.plate_number_raw,
                "plate_number_final": event.plate_number_final,
                "confidence": event.confidence,
                "decision": event.decision,
                "decision_source": event.decision_source,
                "manual_by": event.manual_by_username,
                "manual_reason": event.manual_reason,
                "manual_note": event.manual_note,
                "is_offline_event": event.is_offline_event,
                "evidence_local_path": event.evidence_path,
                "evidence_uploaded_url": None,
            }
            uploader = EventUploadWorker(self.api, device.access_token, event_id, payload)
            uploader.upload_success.connect(self._on_event_uploaded)
            uploader.upload_error.connect(self._on_event_upload_error)
            uploader.finished.connect(lambda: self._cleanup_uploader(uploader))
            self._uploaders.append(uploader)
            uploader.start()

    def _on_event_uploaded(self, event_id: str) -> None:
        self.event_repo.mark_synced(event_id)
        self._refresh_recent_events()

    def _on_event_upload_error(self, event_id: str, error: str) -> None:
        self.event_repo.increment_sync_attempt(event_id, error)
        self._refresh_recent_events()

    def _cleanup_uploader(self, uploader: EventUploadWorker) -> None:
        if uploader in self._uploaders:
            self._uploaders.remove(uploader)

    def _handle_check_status(self) -> None:
        plate_raw, _, _ = self.main_view.get_manual_inputs()
        plate = self._normalize_plate(plate_raw)
        if not plate:
            self.main_view.set_status_result("Enter plate")
            self.main_view.set_presence_hint("-")
            self.main_view.enable_not_found_actions(False)
            return
        self.main_view.set_plate_text(plate)
        local_info = self.allow_repo.get_plate_info(plate)
        presence = self.presence_repo.get_presence(plate)
        if presence:
            self.main_view.set_presence_hint(presence[0])
        else:
            self.main_view.set_presence_hint("UNKNOWN")

        if local_info:
            status, valid_to = local_info
            extra = self._format_valid_to(valid_to)
            self.main_view.set_status_result(f"{status}{extra}")
            self.main_view.enable_not_found_actions(False)
            if self.is_online and self._has_token() and self.main_view.is_check_online():
                self._lookup_online(plate)
            return

        self.main_view.set_status_result("NOT FOUND")
        self.main_view.enable_not_found_actions(True)
        if self.is_online and self._has_token() and self.main_view.is_check_online():
            self._lookup_online(plate)

    def _handle_sync_recheck(self) -> None:
        plate_raw, _, _ = self.main_view.get_manual_inputs()
        plate = self._normalize_plate(plate_raw)
        if not plate:
            return
        self._pending_recheck_plate = plate
        if not self.sync_worker.isRunning():
            self.sync_worker.start()
        self.sync_worker.trigger_sync()

    def _handle_add_temp_permit(self) -> None:
        plate_raw, reason, note = self.main_view.get_manual_inputs()
        plate = self._normalize_plate(plate_raw)
        if not plate:
            QtWidgets.QMessageBox.warning(self, "Temporary Permit", "Plate number is required")
            return
        self.main_view.set_plate_text(plate)

        if self.is_online and self._has_token():
            valid_to = now_ts() + 24 * 3600
            payload = {
                "plate_number": plate,
                "owner_name": "Temporary Permit",
                "permit_status": "ALLOWED",
                "valid_to": valid_to,
            }
            worker = RegisterVehicleWorker(self.api, self._get_token(), payload)
            worker.register_success.connect(self._on_temp_permit_success)
            worker.register_error.connect(self._on_temp_permit_error)
            worker.start()
            self._temp_permit_worker = worker
        else:
            if not reason or not note:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Temporary Permit",
                    "Offline temporary allow requires a manual reason and note.",
                )
                return
            self._create_offline_temporary_allow(plate, reason, note)

    def _on_temp_permit_success(self, data: dict) -> None:
        vehicle = data.get("vehicle") or {}
        plate = vehicle.get("plate_number")
        status = vehicle.get("status", "ALLOWED")
        valid_to = vehicle.get("valid_to")
        if plate:
            self.allow_repo.upsert_items([
                (plate, status, valid_to, now_ts(), now_ts()),
            ])
            self.main_view.set_status_result(f"{status}{self._format_valid_to(valid_to)}")
            self.main_view.enable_not_found_actions(False)
        QtWidgets.QMessageBox.information(self, "Temporary Permit", "Temporary permit added")

    def _on_temp_permit_error(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, "Temporary Permit", f"Failed: {message}")

    def _lookup_online(self, plate: str) -> None:
        worker = LookupWorker(self.api, self._get_token(), plate)
        worker.lookup_success.connect(self._on_lookup_success)
        worker.lookup_not_found.connect(self._on_lookup_not_found)
        worker.lookup_error.connect(self._on_lookup_error)
        worker.start()
        self._lookup_worker = worker

    def _on_lookup_success(self, data: dict) -> None:
        plate = data.get("plate_number", "")
        status = data.get("status", "UNKNOWN")
        valid_to = data.get("valid_to")
        if plate:
            self.allow_repo.upsert_items([
                (plate, status, valid_to, now_ts(), now_ts()),
            ])
        self.main_view.set_status_result(f"{status}{self._format_valid_to(valid_to)}")
        self.main_view.enable_not_found_actions(False)

    def _on_lookup_not_found(self) -> None:
        self.main_view.set_status_result("NOT FOUND (online)")
        self.main_view.enable_not_found_actions(True)

    def _on_lookup_error(self, message: str) -> None:
        self.main_view.set_status_result(f"Lookup error: {message}")
        self.main_view.enable_not_found_actions(True)

    def _create_offline_temporary_allow(self, plate: str, reason: str, note: str) -> None:
        event_id = str(uuid.uuid4())
        ts = now_ts()
        user_profile = self.device_repo.get_user_profile()
        manual_by = user_profile.email if user_profile else None
        event = EventRecord(
            id=event_id,
            event_time=ts,
            gate_id=self.config.gate_id,
            lane_id=self.config.lane_id,
            device_id=self.device.device_id,
            direction=self.config.direction,
            plate_number_raw=plate,
            plate_number_final=plate,
            confidence=None,
            decision="ALLOW",
            decision_source="MANUAL",
            manual_by_username=manual_by,
            manual_reason=reason,
            manual_note=note,
            is_offline_event=True,
            evidence_path=None,
            synced=False,
            sync_attempts=0,
            last_sync_error=None,
            created_at=ts,
        )
        self.event_repo.add_event(event)
        self._update_presence_hint(event.plate_number_final, event.direction)
        self._refresh_recent_events()
        QtWidgets.QMessageBox.information(
            self,
            "Temporary Permit",
            "Offline temporary allow logged. This is not a real permit.",
        )

    def _update_presence_hint(self, plate_number: str, direction: str) -> None:
        state = "INSIDE" if direction == "ENTRY" else "OUTSIDE"
        self.presence_repo.upsert_presence(plate_number, state, now_ts())

    def _has_token(self) -> bool:
        device = self.device_repo.get_device()
        return bool(device and device.access_token)

    def _get_token(self) -> str:
        device = self.device_repo.get_device()
        return device.access_token if device else ""

    @staticmethod
    def _normalize_plate(plate: str) -> str:
        return "".join(plate.strip().upper().split())

    @staticmethod
    def _format_ts(ts: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _format_valid_to(self, valid_to: int | None) -> str:
        if not valid_to:
            return ""
        return f" (valid to {self._format_ts(valid_to)})"

    def _restart_camera_service(self) -> None:
        self.camera_service.stop()
        self.camera_service = CameraService(
            mode=self.config.camera_mode,
            index=self.config.camera_index,
            rtsp_url=self.config.camera_rtsp_url,
        )
        self.camera_service.frame_ready.connect(self.main_view.update_frame)
        self.camera_service.status_changed.connect(self.main_view.set_camera_status)
        self.camera_service.start()


    def _open_settings(self) -> None:
        self.settings_page.load_from_config(self.config)
        self.stack.setCurrentWidget(self.settings_page)

    def _on_settings_saved(self, config: AppConfig) -> None:
        self.config = config
        self.api.config = config
        self.device = self.device_service.ensure_device(
            gate_id=config.gate_id,
            lane_id=config.lane_id,
            device_name=config.device_name,
        )
        self.main_view.set_gate_lane(config.gate_id, config.lane_id)
        self._restart_camera_service()
        self.sync_worker.set_interval(config.sync_interval_seconds)
        self.stack.setCurrentWidget(self.main_view)

    def _handle_settings_cancelled(self) -> None:
        self.stack.setCurrentWidget(self.main_view)

    def _handle_logout(self) -> None:
        self.device_repo.clear_session()
        self.is_online = False
        self.main_view.set_online_status(False)
        self.main_view.set_user("-")
        self.main_view.set_sync_status("Logged out")
        self.next_sync_at = None
        self._update_next_sync_label()
        if self.sync_worker.isRunning():
            self.sync_worker.stop()
            self.sync_worker.wait(2000)
        self.sync_worker = self._create_sync_worker()
        self._connect_sync_signals()
        self.camera_service.stop()
        self.stack.setCurrentWidget(self.login_view)

    def _handle_sync_now(self) -> None:
        if not self.sync_worker.isRunning():
            self.sync_worker.start()
        self.sync_worker.trigger_sync()

    def _update_next_sync_label(self) -> None:
        if not self.next_sync_at:
            self.main_view.set_next_sync("-")
            return
        remaining = max(0, self.next_sync_at - now_ts())
        self.main_view.set_next_sync(f"{remaining}s")

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.main_view.fullscreen_button.setText("Fullscreen")
        else:
            self.showFullScreen()
            self.main_view.fullscreen_button.setText("Exit Fullscreen")

    def closeEvent(self, event) -> None:
        self.camera_service.stop()
        self.sync_worker.stop()
        self.sync_worker.wait(2000)
        self.conn.close()
        event.accept()


def main() -> None:
    setup_logging()
    config = load_config()

    app = QtWidgets.QApplication([])

    # ── Load Poppins font if bundled, otherwise fall back to system ──
    _fonts_dir = Path(__file__).resolve().parent / "assets" / "fonts"
    if _fonts_dir.is_dir():
        for font_file in _fonts_dir.glob("*.ttf"):
            QtGui.QFontDatabase.addApplicationFont(str(font_file))

    # ── Apply SIT branded stylesheet globally ────────────────────
    app.setStyleSheet(SIT_STYLESHEET)

    window = AppWindow(config)
    window.resize(1200, 700)
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
