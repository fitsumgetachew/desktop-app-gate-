from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path

import cv2
import requests
from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.models.domain import EventRecord
from smart_gate.repositories.allowlist_repo import AllowlistRepository
from smart_gate.repositories.db import Database, init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.repositories.event_repo import EventRepository
from smart_gate.repositories.manual_reason_repo import ManualReasonRepository
from smart_gate.repositories.presence_repo import PresenceRepository
from smart_gate.repositories.punch_repo import PunchRepository
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.models.domain import VehicleRecord
from smart_gate.services.alarm_service import AlarmService
from smart_gate.services import attendance_display
from smart_gate.services.barrier_controller import (
    VisualBarrierController,
    safe_signal_open,
)
from smart_gate.services.car_notice import CarNoticeService
from smart_gate.services import enrolment_status
from smart_gate.services.face_camera_service import FaceCameraService
from smart_gate.services.attendance_speech import AttendanceAnnouncer
from smart_gate.services.speech_service import build_speaker
from smart_gate.services.api_client import ApiClient
from smart_gate.services.auth_service import SessionExpiredError
from smart_gate.services.camera_service import CameraService
from smart_gate.services.decision_state import (
    AutoAllowCountdown,
    DecisionState,
    classify,
)
from smart_gate.services.device_service import DeviceService
from smart_gate.services.permit_rules import (
    DECISION_ALLOW,
    PlateAssessment,
    assess_plate,
    blacklist_override_error,
    format_valid_to,
)
from smart_gate.services.sync_service import SyncWorker
from smart_gate.services.token_store import token_store
from smart_gate.services.vehicle_mapping import allowlist_item_to_record, record_to_vehicle
from smart_gate.services.worker_context import worker_context
from smart_gate.ui.login_view import LoginView
from smart_gate.ui.main_view import MainGateView
from smart_gate.ui.registration_dialog import RegistrationDialog
from smart_gate.ui.settings_view import SettingsPage
from smart_gate.ui.staff_enrolment_dialog import StaffEnrolmentDialog
from smart_gate.utils.config import AppConfig, load_config, save_config
from smart_gate.ui.theme import SIT_STYLESHEET
from smart_gate.utils.logging import setup_logging
from smart_gate.utils.paths import ensure_dir
from smart_gate.utils.plates import normalize_plate
from smart_gate.utils.time import now_ts

logger = logging.getLogger(__name__)

# Guard-issued temporary permits are capped at 24 h by the server.
TEMP_PERMIT_SECONDS = 24 * 3600


class LoginWorker(QtCore.QThread):
    # (access_token, device_check) — see _check_device for the dict's shape
    login_success = QtCore.Signal(str, dict)
    login_blocked = QtCore.Signal(str)   # server says this device is not registered
    login_error = QtCore.Signal(str)

    def __init__(
        self,
        config: AppConfig,
        db_path: Path,
        email: str = "",
        password: str = "",
        code: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.email = email
        self.password = password
        # Portal mode: the browser already authenticated the operator and the
        # portal minted this one-time code, so only step 2 is ours to run.
        self.code = code

    def run(self) -> None:
        try:
            with worker_context(self.config, self.db_path) as ctx:
                if self.code is not None:
                    data = ctx.auth.exchange_code(self.code)
                else:
                    # Two-step desktop auth: start → exchange → token in token_store
                    data = ctx.auth.login(self.email, self.password)
                token = data["access_token"]

                device = ctx.device_repo.get_device()

                # Register device (idempotent — safe to call on every login)
                if device:
                    try:
                        DeviceService(ctx.api, ctx.device_repo).register_device(token, device)
                    except Exception as exc:
                        logger.warning("Device registration failed: %s", exc)

                check = self._check_device(ctx.api, token, device)

            if check["registered"] is False:
                self.login_blocked.emit(
                    check.get("message")
                    or "This device is not registered on the server. "
                       "Contact your administrator."
                )
                return

            self.login_success.emit(token, check)
        except Exception as exc:
            logger.exception("Login failed")
            self.login_error.emit(self._error_message(exc))

    def _error_message(self, exc: Exception) -> str:
        """Turn an exchange failure into something the guard can act on.

        Only portal mode is remapped: there the two failures that actually
        happen at a gate — a code that timed out (120 s TTL) and a portal that
        cannot be reached — look identical in ``str(exc)``.
        """
        if self.code is None:
            return str(exc)
        if isinstance(exc, requests.HTTPError):
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                return "Invalid or expired code — generate a new one on the portal page."
            return f"The portal rejected the sign-in (HTTP {status})."
        if isinstance(exc, requests.RequestException):
            return "Cannot reach the portal. Check the network connection and try again."
        return str(exc)

    @staticmethod
    def _check_device(api: ApiClient, token: str, device) -> dict:
        """Resolve device registration, failing *closed* on an explicit refusal.

        Returns ``{"registered": True|False|None, "offline": bool, ...}``:

        * ``registered=False`` — the server answered ``registered: false``.
          Login must be blocked; defaulting to True here let an unknown device
          through.
        * ``registered=None`` with ``offline=True`` — the server was
          unreachable. The gate must keep working, so login proceeds in an
          explicitly-flagged offline mode.
        """
        result: dict = {
            "registered": None,
            "offline": True,
            "gate_id": None,
            "lane_id": None,
            "message": "",
        }
        if not device:
            result["message"] = "No local device identity."
            return result
        try:
            resp = api.check_device(token, device.device_id)
        except requests.HTTPError as exc:
            # The portal has no /devices/check yet. A 404/501 is "this server
            # doesn't do device checks", not "the network is down" — say so, and
            # still proceed offline rather than locking the gate out.
            status = exc.response.status_code if exc.response is not None else None
            logger.warning("Device check returned HTTP %s — offline mode", status)
            if status == 403:
                # The portal rejects a request whose device_id differs from the
                # one these credentials were issued for. Sliding into offline
                # mode would hide a condition the operator can actually fix, so
                # fail closed and send them back to sign in from this machine.
                result["offline"] = False
                result["registered"] = False
                result["message"] = (
                    "These credentials were issued for a different device. "
                    "Sign in again from this machine."
                )
                return result
            if status in (404, 501):
                result["message"] = "device check not available — continuing in offline mode"
            else:
                result["message"] = f"Device check failed (HTTP {status})."
            return result
        except requests.RequestException as exc:
            logger.warning("Device check unreachable — offline mode: %s", exc)
            result["message"] = f"Server unreachable ({exc.__class__.__name__})."
            return result
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Device check failed — offline mode: %s", exc)
            result["message"] = str(exc)
            return result

        result["offline"] = False
        result["registered"] = bool(resp.get("registered"))
        result["message"] = resp.get("message") or ""
        gate = resp.get("gate") or {}
        lane = resp.get("lane") or {}
        result["gate_id"] = gate.get("id")
        result["lane_id"] = lane.get("id")
        result["gate_name"] = gate.get("name")
        result["lane_name"] = lane.get("name")
        return result


class LogoutWorker(QtCore.QThread):
    """Runs ``AuthService.logout()`` off the UI thread.

    In portal mode logout posts ``/auth/logout`` to revoke the session
    server-side; that call must never freeze the gate screen, and it is
    best-effort, so nothing is reported back.
    """

    def __init__(self, config: AppConfig, db_path: Path) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path

    def run(self) -> None:
        try:
            with worker_context(self.config, self.db_path) as ctx:
                ctx.auth.logout()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Logout cleanup failed: %s", exc)


class LookupWorker(QtCore.QThread):
    lookup_success = QtCore.Signal(dict)
    lookup_not_found = QtCore.Signal()
    lookup_error = QtCore.Signal(str)
    auth_expired = QtCore.Signal()

    def __init__(self, config: AppConfig, db_path: Path, plate_number: str) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.plate_number = plate_number

    def run(self) -> None:
        try:
            with worker_context(self.config, self.db_path) as ctx:
                data = ctx.auth.call_authed(
                    lambda token: ctx.api.lookup_vehicle(token, self.plate_number)
                )
            self.lookup_success.emit(data)
        except SessionExpiredError:
            self.auth_expired.emit()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                self.lookup_not_found.emit()
            else:
                self.lookup_error.emit(str(exc))
        except Exception as exc:
            self.lookup_error.emit(str(exc))


class TempPermitWorker(QtCore.QThread):
    """Issues a guard-scoped temporary permit via POST /permits/temporary."""

    permit_success = QtCore.Signal(dict)
    permit_conflict = QtCore.Signal(str)   # 409 — plate is blacklisted
    permit_error = QtCore.Signal(str)
    auth_expired = QtCore.Signal()

    def __init__(self, config: AppConfig, db_path: Path, payload: dict) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.payload = payload

    def run(self) -> None:
        try:
            with worker_context(self.config, self.db_path) as ctx:
                data = ctx.auth.call_authed(
                    lambda token: ctx.api.create_temporary_permit(token, self.payload)
                )
            self.permit_success.emit(data or {})
        except SessionExpiredError:
            self.auth_expired.emit()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 409:
                self.permit_conflict.emit(_error_detail(exc))
            elif status_code == 403:
                self.permit_error.emit(
                    "Your account is not allowed to issue temporary permits."
                )
            else:
                self.permit_error.emit(_error_detail(exc) or str(exc))
        except Exception as exc:
            self.permit_error.emit(str(exc))


class RegisterVisitorWorker(QtCore.QThread):
    """On-the-spot registration via POST /vehicles/register-visitor.

    Online-only: a network failure is reported as such and nothing is written
    locally, so a registration never exists only on this one gate PC.
    """

    register_success = QtCore.Signal(dict)
    register_conflict = QtCore.Signal(str)   # 409 — plate is blacklisted
    register_offline = QtCore.Signal(str)    # server unreachable
    register_error = QtCore.Signal(str)
    auth_expired = QtCore.Signal()

    def __init__(self, config: AppConfig, db_path: Path, payload: dict) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.payload = payload

    def run(self) -> None:
        try:
            with worker_context(self.config, self.db_path) as ctx:
                data = ctx.auth.call_authed(
                    lambda token: ctx.api.register_visitor(token, self.payload)
                )
            self.register_success.emit(data or {})
        except SessionExpiredError:
            self.auth_expired.emit()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 409:
                self.register_conflict.emit(_error_detail(exc))
            elif status_code == 403:
                self.register_error.emit(
                    "Your account is not allowed to register vehicles."
                )
            else:
                self.register_error.emit(_error_detail(exc) or str(exc))
        except requests.RequestException as exc:
            logger.warning("Visitor registration unreachable: %s", exc)
            self.register_offline.emit(
                "Registration requires a server connection. Nothing was saved — "
                "use 'Add Temporary Permit' for a local, offline allow."
            )
        except Exception as exc:
            self.register_error.emit(str(exc))


def _error_detail(exc: requests.HTTPError) -> str:
    """Pull the server's ``detail``/``message`` out of an error response."""
    if exc.response is None:
        return str(exc)
    try:
        body = exc.response.json()
    except ValueError:
        return exc.response.text.strip() or str(exc)
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return str(exc)


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
        # Read-only on this thread: the car notice is one indexed lookup
        # inside the decision path, and the punch counters feed the panel.
        self.staff_repo = StaffRepository(self.conn)
        self.punch_repo = PunchRepository(self.conn)

        # UI-thread client: local device bookkeeping only. Every HTTP call is
        # made by a worker with its own ApiClient (see services/worker_context).
        self.api = ApiClient(config)
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

        # ── Staff attendance ─────────────────────────────────────────
        # Every piece is optional. With FACE_ATTENDANCE_ENABLED=false no webcam
        # thread is created at all and the screen falls back to today's
        # single-column gate layout — that is the configuration a gate PC
        # without a working dlib build runs, so it stays a first-class path.
        self.attendance_enabled = bool(
            getattr(config, "face_attendance_enabled", False)
        )
        self.face_service: FaceCameraService | None = None
        if self.attendance_enabled:
            self.face_service = FaceCameraService(config, self.db.db_path)
        self.speaker = build_speaker(self.attendance_enabled)
        # Decides whether an outcome is worth saying out loud; the phrases
        # themselves live in services/attendance_speech.py.
        self.announcer = AttendanceAnnouncer()
        self.car_notice = CarNoticeService(self.staff_repo, self.punch_repo)
        # Visual only for now; a later phase swaps in the serial transport
        # behind the same signal_open() call. See barrier_controller.
        self.barrier = VisualBarrierController()

        self.sync_worker = self._create_sync_worker()

        self.is_online = False
        self.offline_mode = False   # signed in without reaching the server
        self.last_capture_path: str | None = None
        self.login_worker: LoginWorker | None = None
        self.logout_worker: LogoutWorker | None = None
        self._lookup_workers: list[LookupWorker] = []
        self._temp_permit_workers: list[TempPermitWorker] = []
        self._register_workers: list[RegisterVisitorWorker] = []
        self._pending_recheck_plate: str | None = None
        self._last_ai_result: dict | None = None  # populated by ALPR pipeline
        self._assessment: PlateAssessment | None = None  # status of the plate on screen

        # ── Traffic-light decision state ─────────────────────────────
        self._decision_state: DecisionState | None = None
        # Plates the guard has already responded to, so a re-detection of the
        # same vehicle does not undo their decision.
        self._auto_allow_declined: str | None = None   # STOP pressed
        self._alarm_acked_plate: str | None = None     # siren acknowledged
        self.alarm_service = AlarmService(parent=self)
        self.countdown = AutoAllowCountdown(config.auto_allow_seconds)
        self.countdown_timer = QtCore.QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._on_countdown_tick)
        self._registration_dialog: RegistrationDialog | None = None
        self._enrolment_dialog: StaffEnrolmentDialog | None = None

        self.stack = QtWidgets.QStackedWidget()
        # The SSO link carries the device_id the code is bound to, so the login
        # view is built after ensure_device() above.
        self.login_view = LoginView(
            auth_mode=config.auth_mode,
            portal_sso_url=config.portal_sso_url,
            device_id=self.device.device_id,
        )
        self.main_view = MainGateView(attendance_enabled=self.attendance_enabled)
        # The controller flashes the indicator through the view; wired here
        # rather than in the view so the decision path stays testable
        # without Qt.
        self.barrier._on_signal = self.main_view.flash_barrier_signal
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
        self.refresh_timer.timeout.connect(self._refresh_punch_count)
        self.refresh_timer.timeout.connect(self._refresh_enrolment_status)
        self.refresh_timer.start()

        self.next_sync_at: int | None = None
        self.sync_countdown_timer = QtCore.QTimer(self)
        self.sync_countdown_timer.setInterval(1000)
        self.sync_countdown_timer.timeout.connect(self._update_next_sync_label)
        self.sync_countdown_timer.start()

    def _connect_signals(self) -> None:
        self.login_view.login_requested.connect(self._handle_login)
        self.login_view.code_submitted.connect(self._handle_portal_code)
        self.camera_service.frame_ready.connect(self.main_view.update_frame)
        self.camera_service.status_changed.connect(self.main_view.set_camera_status)
        self.camera_service.plate_detected.connect(self._on_plate_detected)
        self.main_view.decision_requested.connect(self._handle_decision)
        self.main_view.capture_requested.connect(self._handle_capture)
        self.main_view.settings_requested.connect(self._open_settings)
        self.main_view.logout_requested.connect(self._handle_logout)
        self.main_view.sync_now_requested.connect(self._handle_sync_now)
        self.main_view.check_status_requested.connect(self._handle_check_status)
        self.main_view.sync_recheck_requested.connect(self._handle_sync_recheck)
        self.main_view.add_temp_permit_requested.connect(self._handle_add_temp_permit)
        self.main_view.fullscreen_requested.connect(self._toggle_fullscreen)
        self.main_view.auto_allow_cancelled.connect(self._on_auto_allow_cancelled)
        self.main_view.alarm_acknowledged.connect(self._on_alarm_acknowledged)
        self.main_view.register_vehicle_requested.connect(self._open_registration_dialog)
        self.main_view.staff_details_requested.connect(self._open_staff_enrolment)
        if self.face_service is not None:
            self.face_service.frame_ready.connect(
                self.main_view.update_attendance_frame
            )
            self.face_service.status_changed.connect(
                self.main_view.set_attendance_camera_status
            )
            self.face_service.detection_changed.connect(
                self.main_view.set_face_detection
            )
            self.face_service.face_unrecognised.connect(self._on_face_unrecognised)
            self.face_service.punch_recorded.connect(self._on_punch_recorded)
            self.face_service.punch_suppressed.connect(self._on_punch_suppressed)
        self.settings_page.settings_saved.connect(self._on_settings_saved)
        self.settings_page.settings_cancelled.connect(self._handle_settings_cancelled)
        self._connect_sync_signals()

    def _connect_sync_signals(self) -> None:
        self.sync_worker.online_changed.connect(self._handle_online)
        self.sync_worker.sync_status.connect(self._handle_sync_status)
        self.sync_worker.last_sync_time.connect(self._handle_last_sync_time)
        self.sync_worker.next_sync_time.connect(self._handle_next_sync_time)
        self.sync_worker.auth_required.connect(self._handle_auth_required)
        self.sync_worker.device_deprovisioned.connect(self._handle_device_deprovisioned)

    def _create_sync_worker(self) -> SyncWorker:
        # The worker builds its own ApiClient (and requests.Session) inside
        # run(), so nothing HTTP is shared with the UI thread.
        worker = SyncWorker(
            config=self.config,
            db_path=self.db.db_path,
            interval_seconds=self.config.sync_interval_seconds,
        )
        return worker

    def _handle_login(self, email: str, password: str) -> None:
        self._start_login_worker(
            LoginWorker(self.config, self.db.db_path, email, password),
            "Logging in...",
        )

    def _handle_portal_code(self, code: str) -> None:
        """Portal mode — exchange the one-time code the operator pasted.

        The code itself is never logged; only that an exchange was attempted.
        """
        self._start_login_worker(
            LoginWorker(self.config, self.db.db_path, code=code),
            "Signing in with the portal code...",
        )

    def _start_login_worker(self, worker: LoginWorker, status: str) -> None:
        if self.login_worker is not None and self.login_worker.isRunning():
            return
        self.login_view.set_busy(True)
        self.login_view.set_status(status)
        self.login_worker = worker
        self.login_worker.login_success.connect(self._on_login_success)
        self.login_worker.login_blocked.connect(self._on_login_blocked)
        self.login_worker.login_error.connect(self._on_login_error)
        self.login_worker.finished.connect(lambda: self.login_view.set_busy(False))
        self.login_worker.start()

    def _on_login_success(self, token: str, device_check: dict) -> None:
        self.login_view.set_status("Login success")
        self.login_view.clear_credentials()

        self.offline_mode = bool(device_check.get("offline"))
        self.main_view.set_offline_mode(
            self.offline_mode,
            "Offline mode — device registration could not be verified "
            f"({device_check.get('message') or 'server unreachable'}). "
            "Decisions are queued locally and synced when the server returns.",
        )
        if not self.offline_mode:
            self._apply_server_assignment(
                device_check.get("gate_id"), device_check.get("lane_id")
            )

        user_profile = self.device_repo.get_user_profile()
        if user_profile:
            self.main_view.set_user(user_profile.email, user_profile.role)
        self.main_view.set_gate_lane(
            self.config.gate_id,
            self.config.lane_id,
            device_check.get("gate_name"),
            device_check.get("lane_name"),
        )
        self.stack.setCurrentWidget(self.main_view)
        self.camera_service.start()
        self._start_face_service()
        if not self.sync_worker.isRunning():
            self.sync_worker.start()
        else:
            self.sync_worker.trigger_sync()
        self._refresh_manual_reasons()
        self._refresh_recent_events()

    def _apply_server_assignment(self, gate_id: str | None, lane_id: str | None) -> None:
        """Adopt the gate/lane the server assigned to this device.

        The server's assignment is authoritative — a locally-edited GATE_ID must
        not silently mislabel every event this lane produces.
        """
        if not gate_id and not lane_id:
            return
        new_gate = gate_id or self.config.gate_id
        new_lane = lane_id or self.config.lane_id
        if new_gate == self.config.gate_id and new_lane == self.config.lane_id:
            return

        logger.warning(
            "Server device assignment differs from local config: "
            "local %s/%s → server %s/%s. Applying the server assignment.",
            self.config.gate_id, self.config.lane_id, new_gate, new_lane,
        )
        self.config.gate_id = new_gate
        self.config.lane_id = new_lane
        try:
            save_config(self.config)
        except OSError as exc:
            logger.warning("Could not persist server gate/lane assignment: %s", exc)
        self.device_repo.update_gate_lane(self.device.device_id, new_gate, new_lane)
        self.device = self.device_service.ensure_device(
            gate_id=new_gate,
            lane_id=new_lane,
            device_name=self.config.device_name,
        )
        self.sync_worker.update_config(self.config)

    def _on_login_blocked(self, message: str) -> None:
        """Server explicitly refused this device — do not let the guard in."""
        logger.warning("Login blocked: %s", message)
        token_store.clear()
        self.device_repo.clear_session()
        self.login_view.set_status(message)
        QtWidgets.QMessageBox.critical(self, "Device Not Registered", message)

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
        try:
            reasons = self.reason_repo.list_active()
        except sqlite3.ProgrammingError:
            return  # shutting down: the connection is already closed
        if not reasons:
            reasons = ["Manual override"]
        self.main_view.set_reasons(reasons)

    def _refresh_recent_events(self) -> None:
        try:
            rows = self.event_repo.list_recent()
        except sqlite3.ProgrammingError:
            return  # shutting down: the connection is already closed
        self.main_view.set_recent_events(rows)

    def _handle_capture(self) -> None:
        frame = self.camera_service.capture_current_frame()
        if frame is None:
            QtWidgets.QMessageBox.warning(self, "Capture", "No camera frame available")
            return
        path = self._save_frame(frame, suffix="capture")
        self.last_capture_path = path
        QtWidgets.QMessageBox.information(self, "Capture", f"Saved to {path}")

    def _on_plate_detected(
        self,
        plate_number: str,
        confidence: float,
        raw_text: str,
        ocr_confidence: float,
        crop,
    ) -> None:
        """Called from the camera worker thread (via Qt signal) when ALPR commits a plate.

        Runs on the UI thread — Qt queues the cross-thread signal — so it is
        safe to touch widgets, timers and the sound effect from here.
        """
        plate = normalize_plate(plate_number)
        if not plate:
            return

        # A different plate mid-countdown means the previous vehicle is gone:
        # cancel and re-evaluate rather than opening the barrier for a car that
        # is no longer at the gate.
        if self.countdown.on_plate_committed(plate):
            logger.info(
                "Auto-allow countdown cancelled — a different plate (%s) was detected", plate
            )
            self._stop_countdown()

        # Save the plate crop as evidence for the upcoming decision
        evidence_path = None
        try:
            evidence_path = self._save_frame(crop, suffix=f"ai_{plate}")
        except Exception:
            logger.warning("Failed to save ALPR crop", exc_info=True)

        self._last_ai_result = {
            "plate": plate,
            "raw_text": raw_text,
            "confidence": confidence,
            "ocr_confidence": ocr_confidence,
            "evidence_path": evidence_path,
        }
        self.main_view.set_plate_detected(plate, confidence)
        # Resolve the plate against the local cache straight away so a
        # blacklisted vehicle raises the alarm without the guard clicking.
        self._handle_check_status()

    # ------------------------------------------------------------------
    # Traffic-light decision state
    # ------------------------------------------------------------------

    def _apply_decision_state(self, plate: str, vehicle: VehicleRecord | None) -> DecisionState:
        """Classify a plate and drive the camera view, alarm and countdown.

        Re-entrant: the ALPR re-commits the same plate every few frames and a
        lookup can land afterwards, so this must be idempotent for a plate the
        guard has already responded to — a re-detection must not restart a
        cancelled countdown or re-sound an acknowledged siren.
        """
        state = classify(plate, vehicle, fmt_ts=self._format_ts)
        same_plate = (
            self._decision_state is not None and self._decision_state.plate == state.plate
        )
        if not same_plate:
            # A different vehicle: the previous guard's STOP / acknowledge no
            # longer applies.
            self._auto_allow_declined = None
            self._alarm_acked_plate = None
        self._decision_state = state
        self.main_view.set_decision_state(state)

        # ── Alarm ────────────────────────────────────────────────────
        if state.alarm:
            if self._alarm_acked_plate == state.plate:
                self.main_view.set_alarm_acknowledged()   # stay silent
            else:
                self.alarm_service.start()
        else:
            self.alarm_service.stop()

        # ── Auto-allow countdown ─────────────────────────────────────
        # Only GREEN may auto-continue, only when configured, and only if the
        # guard has not already said no for this plate.
        eligible = (
            state.can_auto_allow
            and self.countdown.enabled
            and self._auto_allow_declined != state.plate
        )
        if not eligible:
            self._stop_countdown()
            return state

        if self.countdown.active and self.countdown.plate == state.plate:
            # Already counting down for this very plate — let it run out
            # instead of resetting the clock on every frame.
            self.main_view.set_countdown(self.countdown.remaining)
            return state

        self._stop_countdown()
        if self.countdown.start(state.plate):
            self.main_view.set_countdown(self.countdown.remaining)
            self.countdown_timer.start()
        return state

    def _on_countdown_tick(self) -> None:
        if not self.countdown.active:
            self._stop_countdown()
            return
        fired = self.countdown.tick()
        if fired:
            plate = self._decision_state.plate if self._decision_state else ""
            self._stop_countdown()
            logger.info("Auto-allow countdown elapsed for %s — confirming ALLOW", plate)
            # This is where the barrier-open serial command will go once the
            # microcontroller is wired up.
            self._submit_decision(DECISION_ALLOW, source="AUTO", auto_confirmed=True)
            return
        self.main_view.set_countdown(self.countdown.remaining)

    def _stop_countdown(self) -> None:
        self.countdown_timer.stop()
        self.countdown.cancel()
        self.main_view.set_countdown(0)

    def _on_auto_allow_cancelled(self) -> None:
        """STOP pressed — drop to the normal manual ALLOW/DENY flow."""
        plate = self._decision_state.plate if self._decision_state else None
        logger.info("Guard cancelled the auto-allow countdown for %s", plate)
        self._stop_countdown()
        # Latch the refusal: re-detecting this same vehicle must not silently
        # start a fresh countdown behind the guard's back.
        self._auto_allow_declined = plate
        if self._decision_state is not None:
            self.main_view.set_decision_state(self._decision_state)
        self.main_view.set_status_result("Auto-open cancelled — decide manually", level="warn")

    def _on_alarm_acknowledged(self) -> None:
        """Silence the siren; the red state and the override rules stay put."""
        plate = self._decision_state.plate if self._decision_state else None
        logger.info("Blacklist alarm acknowledged by the guard for %s", plate)
        self.alarm_service.stop()
        self._alarm_acked_plate = plate
        self.main_view.set_alarm_acknowledged()

    def _clear_decision_state(self) -> None:
        """Full reset: border, banner, countdown and siren all go away."""
        self._stop_countdown()
        self.alarm_service.stop()
        self._decision_state = None
        self._auto_allow_declined = None
        self._alarm_acked_plate = None
        self.main_view.clear_decision_state()

    def _save_frame(self, frame, suffix: str) -> str:
        evidence_dir = ensure_dir(Path(self.config.evidence_dir))
        filename = f"{now_ts()}_{suffix}.jpg"
        full_path = evidence_dir / filename
        cv2.imwrite(str(full_path), frame)
        return str(full_path)

    def _handle_decision(self, decision: str) -> None:
        """ALLOW / DENY pressed by the guard — always a MANUAL decision."""
        self._stop_countdown()
        self._submit_decision(decision, source="MANUAL")

    def _submit_decision(
        self,
        decision: str,
        source: str,
        auto_confirmed: bool = False,
    ) -> None:
        """Record a gate decision.

        ``source`` is ``"AUTO"`` only when the app decided on its own (the
        green countdown elapsing); every button press is ``"MANUAL"``, even
        when the ALPR read the plate — the human made the call.
        """
        plate_raw, reason, note = self.main_view.get_manual_inputs()
        plate = normalize_plate(plate_raw)
        if auto_confirmed and self._decision_state is not None:
            # Trust the state the countdown belonged to, not whatever is in
            # the text field by the time the timer fires.
            plate = self._decision_state.plate
        if not plate:
            QtWidgets.QMessageBox.warning(self, "Decision", "Plate number is required")
            return
        self.main_view.set_plate_text(plate)

        # Re-resolve against the cache: the assessment on screen may belong to a
        # different plate if the guard retyped the field, and a permit can lapse
        # between the status check and the decision.
        assessment = self._assess_from_cache(plate)
        self._assessment = assessment
        self._apply_assessment_to_view(assessment)

        if not self._confirm_blacklist_override(assessment, decision, reason, note):
            return

        ai = self._last_ai_result

        # Evidence: prefer the AI crop (already saved); fall back to live frame capture
        evidence_path = ai["evidence_path"] if ai and ai.get("evidence_path") else None
        if evidence_path is None:
            frame = self.camera_service.capture_current_frame()
            if frame is not None:
                evidence_path = self._save_frame(frame, suffix=plate.replace(" ", ""))

        # plate_number_raw: use the raw OCR text if AI ran, else the typed value
        plate_number_raw = ai["raw_text"] if ai else plate_raw

        # confidence: from AI pipeline if available
        confidence = ai["ocr_confidence"] if ai else None

        decision_source = source

        user_profile = self.device_repo.get_user_profile()
        manual_by_user_id = user_profile.uuid if user_profile else None
        manual_by_username = user_profile.email if user_profile else None

        # Look up the reason id from the local cache so it can be sent to the server
        manual_reason_id = self.reason_repo.get_id_by_text(reason) if reason else None

        event_id = str(uuid.uuid4())
        ts = now_ts()
        event = EventRecord(
            id=event_id,
            event_time=ts,
            gate_id=self.config.gate_id,
            lane_id=self.config.lane_id,
            device_id=self.device.device_id,
            direction=self.config.direction,
            plate_number_raw=plate_number_raw,
            plate_number_final=plate,
            confidence=confidence,
            decision=decision,
            decision_source=decision_source,
            manual_by_user_id=manual_by_user_id,
            manual_by_username=manual_by_username,
            manual_reason_id=manual_reason_id,
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

        # ── Post-decision side effects ───────────────────────────────
        # Both run AFTER the event row is committed and neither may affect it.
        # The gate has already decided by this point; nothing below is allowed
        # to block, delay or undo that.
        if decision == "ALLOW":
            safe_signal_open(self.barrier)
        self._maybe_notify_missing_attendance(event)

        # Reset ALPR buffer and traffic-light state for the next vehicle
        self._last_ai_result = None
        self._assessment = None
        self.camera_service.reset_recognizer()
        self.main_view.clear_plate_detected()
        self._clear_decision_state()

        # Trigger SyncWorker immediately — it owns all uploads (no double-submission risk)
        if self.is_online:
            if not self.sync_worker.isRunning():
                self.sync_worker.start()
            self.sync_worker.trigger_sync()

    # ------------------------------------------------------------------
    # Plate status
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Staff attendance
    # ------------------------------------------------------------------

    def _load_enrolment(self):
        """Read the roster's enrolment state. Returns (staff, summary)."""
        staff = enrolment_status.from_rows(self.staff_repo.enrolment_rows())
        return staff, enrolment_status.summarise(staff)

    def _refresh_enrolment_status(self) -> None:
        """Keep the strip — and the dialog, if open — current.

        Cheap enough for the 5 s timer: one query with correlated sub-counts.
        """
        if not self.attendance_enabled:
            return
        try:
            staff, summary = self._load_enrolment()
        except Exception:
            logger.debug("Could not read enrolment status", exc_info=True)
            return
        text, level = enrolment_status.headline(summary)
        self.main_view.set_enrolment_status(text, level)
        if self._enrolment_dialog is not None and self._enrolment_dialog.isVisible():
            self._enrolment_dialog.set_enrolment(staff, summary)

    def _open_staff_enrolment(self) -> None:
        if self._enrolment_dialog is None:
            self._enrolment_dialog = StaffEnrolmentDialog(self)
            self._enrolment_dialog.refresh_requested.connect(self._sync_staff_now)
        try:
            staff, summary = self._load_enrolment()
        except Exception:
            logger.warning("Could not load staff enrolment", exc_info=True)
            return
        self._enrolment_dialog.set_enrolment(staff, summary)
        self._enrolment_dialog.show()
        self._enrolment_dialog.raise_()

    def _sync_staff_now(self) -> None:
        """Ask for a sync, then repaint once it has had a chance to land."""
        if self.is_online:
            if not self.sync_worker.isRunning():
                self.sync_worker.start()
            self.sync_worker.trigger_sync()
        QtCore.QTimer.singleShot(1500, self._refresh_enrolment_status)

    def _maybe_notify_missing_attendance(self, event: EventRecord) -> None:
        """Remind a staff member who drove in without recording attendance.

        Wrapped whole: this is a convenience bolted onto the decision path, and
        a failure in a local lookup, a banner or a speech engine must never
        surface as a gate error. The decision is already recorded either way.
        """
        if not self.attendance_enabled:
            return
        try:
            notice = self.car_notice.notice_for(
                event.plate_number_final, event.decision, event.direction
            )
            if notice is None:
                return
            self.main_view.show_attendance_notice(notice.banner_text)
            # say() only enqueues — the speaking happens on the speech
            # service's own thread, so the UI never waits on an utterance.
            self.speaker.say(notice.speech_text)
        except Exception:
            logger.warning(
                "Attendance reminder failed — the gate decision stands",
                exc_info=True,
            )

    def _announce(self, status, full_name: str | None = None) -> None:
        """Speak an outcome, if this one is worth speaking.

        The announcer suppresses repeats — a face held in front of the camera
        produces a verdict several times a second, and saying it every time is
        how a helpful prompt turns into something staff unplug. Wrapped whole:
        attendance must never be able to break on a speech engine.
        """
        try:
            phrase = self.announcer.announce(status, full_name)
            if phrase:
                self.speaker.say(phrase)
        except Exception:
            logger.warning("Attendance announcement failed", exc_info=True)

    def _on_face_unrecognised(self) -> None:
        """A face the index could not place. Neutral, and never an alarm —
        this is attendance, not security."""
        self.main_view.apply_attendance_state(attendance_display.unrecognised())
        self._announce(attendance_display.AttendanceStatus.UNRECOGNISED)

    def _on_punch_recorded(self, staff_uid: str, full_name: str, punch_time: int) -> None:
        self.main_view.apply_attendance_state(
            attendance_display.recognised(full_name, punch_time, staff_uid)
        )
        self._announce(attendance_display.AttendanceStatus.RECOGNISED, full_name)
        # They have now recorded attendance, so drop any reminder aimed at them
        # and re-arm it for a future day.
        self.car_notice.forget(staff_uid)
        self.main_view.clear_attendance_notice()
        self._refresh_punch_count()

    def _on_punch_suppressed(self, staff_uid: str, full_name: str, since: int) -> None:
        self.main_view.apply_attendance_state(
            attendance_display.suppressed(full_name, since, staff_uid)
        )
        self._announce(attendance_display.AttendanceStatus.SUPPRESSED, full_name)

    def _refresh_punch_count(self) -> None:
        if not self.attendance_enabled:
            return
        try:
            self.main_view.set_attendance_count(self.punch_repo.punch_count_today())
        except Exception:
            logger.debug("Could not refresh the attendance count", exc_info=True)

    def _start_face_service(self) -> None:
        if self.face_service is not None:
            self.face_service.start()
            self._refresh_punch_count()
            self._refresh_enrolment_status()

    def _stop_face_service(self) -> None:
        if self.face_service is not None:
            self.face_service.stop()

    def _assess_vehicle(self, plate: str, vehicle: VehicleRecord | None) -> PlateAssessment:
        """Resolve a plate + its cached record into a permit assessment.

        Applies expiry, not-yet-valid and blacklist rules, so a lapsed permit
        reads as EXPIRED even with no network.
        """
        if vehicle is None:
            return assess_plate(plate, None, found=False)
        return assess_plate(
            plate,
            vehicle.status,
            vehicle.valid_to,
            alert=vehicle.alert,
            valid_from=vehicle.valid_from,
        )

    def _assess_from_cache(self, plate: str) -> PlateAssessment:
        return self._assess_vehicle(plate, self.allow_repo.get_vehicle(plate))

    def _apply_assessment_to_view(self, assessment: PlateAssessment) -> None:
        """Render the compact status line beside the plate field.

        The loud part of the UI — border, banner, siren, countdown — is driven
        separately by :meth:`_apply_decision_state`.
        """
        if not assessment.found:
            self.main_view.set_status_result("NOT FOUND", level="warn")
            self.main_view.enable_not_found_actions(True)
            return

        suffix = format_valid_to(assessment.valid_to, self._format_ts)
        if assessment.blacklisted:
            self.main_view.set_status_result("BLACKLISTED", level="alarm")
        elif assessment.expired:
            self.main_view.set_status_result(f"EXPIRED{suffix}", level="warn")
        elif assessment.not_yet_valid:
            when = self._format_ts(assessment.valid_from) if assessment.valid_from else "later"
            self.main_view.set_status_result(f"NOT YET VALID (from {when})", level="warn")
        else:
            level = "normal" if assessment.allowed else "warn"
            self.main_view.set_status_result(f"{assessment.status}{suffix}", level=level)

        # A permit outside its validity window is effectively "no valid permit"
        # — offer the temporary-permit / re-sync actions, as for an unknown plate.
        self.main_view.enable_not_found_actions(assessment.outside_validity_window)

    def _evaluate_plate(self, plate: str, vehicle: VehicleRecord | None) -> None:
        """Single entry point: assess a plate and refresh every part of the UI."""
        assessment = self._assess_vehicle(plate, vehicle)
        self._assessment = assessment
        self._apply_assessment_to_view(assessment)
        self._apply_decision_state(plate, vehicle)

    def _handle_check_status(self) -> None:
        plate_raw, _, _ = self.main_view.get_manual_inputs()
        plate = normalize_plate(plate_raw)
        if not plate:
            self.main_view.set_status_result("Enter plate")
            self.main_view.set_presence_hint("-")
            self.main_view.enable_not_found_actions(False)
            self._assessment = None
            self._clear_decision_state()
            return
        self.main_view.set_plate_text(plate)

        presence = self.presence_repo.get_presence(plate)
        self.main_view.set_presence_hint(presence[0] if presence else "UNKNOWN")

        self._evaluate_plate(plate, self.allow_repo.get_vehicle(plate))

        if self.is_online and self._has_token() and self.main_view.is_check_online():
            self._lookup_online(plate)

    def _handle_sync_recheck(self) -> None:
        plate_raw, _, _ = self.main_view.get_manual_inputs()
        plate = normalize_plate(plate_raw)
        if not plate:
            return
        self._pending_recheck_plate = plate
        if not self.sync_worker.isRunning():
            self.sync_worker.start()
        self.sync_worker.trigger_sync()

    # ------------------------------------------------------------------
    # Temporary permits
    # ------------------------------------------------------------------

    def _handle_add_temp_permit(self) -> None:
        plate_raw, reason, note = self.main_view.get_manual_inputs()
        plate = normalize_plate(plate_raw)
        if not plate:
            QtWidgets.QMessageBox.warning(self, "Temporary Permit", "Plate number is required")
            return
        self.main_view.set_plate_text(plate)

        assessment = self._assess_from_cache(plate)
        if assessment.blacklisted:
            QtWidgets.QMessageBox.critical(
                self,
                "Temporary Permit",
                f"{plate} is BLACKLISTED. A temporary permit cannot be issued.",
            )
            return

        if self.is_online and self._has_token():
            payload = {
                "plate_number": plate,
                "owner_name": "Temporary Permit",
                "reason_id": self.reason_repo.get_id_by_text(reason) if reason else None,
                "reason_text": reason or None,
                "note": note or None,
                "expires_in_seconds": TEMP_PERMIT_SECONDS,
            }
            worker = TempPermitWorker(self.config, self.db.db_path, payload)
            worker.permit_success.connect(self._on_temp_permit_success)
            worker.permit_conflict.connect(self._on_temp_permit_conflict)
            worker.permit_error.connect(self._on_temp_permit_error)
            worker.auth_expired.connect(self._handle_auth_required)
            worker.finished.connect(lambda: self._cleanup_worker(self._temp_permit_workers, worker))
            self._temp_permit_workers.append(worker)
            worker.start()
        else:
            if not reason or not note:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Temporary Permit",
                    "Offline temporary allow requires a manual reason and note.",
                )
                return
            self._create_offline_temporary_allow(plate, reason, note)

    @staticmethod
    def _permit_body(data: dict) -> dict:
        """Unwrap the vehicle record from the /permits/temporary envelope.

        The response is ``{ok, vehicle, permit}``; ``vehicle`` carries the
        status/valid_to/alert the cache needs, ``permit`` only the grant
        metadata — so prefer the former and fall back for other shapes.
        """
        for key in ("vehicle", "permit", "item"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data

    def _on_temp_permit_success(self, data: dict) -> None:
        body = dict(self._permit_body(data or {}))
        body.setdefault("status", "ALLOWED")
        if not body.get("valid_to") and body.get("expires_at"):
            body["valid_to"] = body["expires_at"]
        plate = normalize_plate(body.get("plate_number", ""))
        if plate:
            vehicle = self._cache_vehicle_payload(body)
            self._evaluate_plate(plate, vehicle)
        QtWidgets.QMessageBox.information(
            self,
            "Temporary Permit",
            "Temporary permit issued — valid for up to 24 hours. "
            "For a longer stay, use Register Vehicle instead.",
        )

    def _on_temp_permit_conflict(self, message: str) -> None:
        """409 — the server refuses a permit for a blacklisted plate."""
        plate_raw, _, _ = self.main_view.get_manual_inputs()
        plate = normalize_plate(plate_raw)
        detail = message or "The plate is blacklisted."
        # The server knows something the cache did not — record the blacklist
        # and raise the alarm before telling the guard.
        if plate:
            vehicle = self._cache_vehicle_payload({
                "plate_number": plate,
                "status": "BLACKLISTED",
                "alert": True,
            })
            self._evaluate_plate(plate, vehicle)
        QtWidgets.QMessageBox.critical(
            self,
            "Temporary Permit Refused",
            f"No temporary permit can be issued for {plate}.\n\n{detail}",
        )

    def _on_temp_permit_error(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, "Temporary Permit", f"Failed: {message}")

    # ------------------------------------------------------------------
    # Online lookup
    # ------------------------------------------------------------------

    def _cache_vehicle_payload(self, payload: dict) -> VehicleRecord | None:
        """Store a server vehicle object (lookup / permit / registration).

        Keeps the richer owner and vehicle fields, and returns the record so the
        caller can drive the UI from exactly what was cached.
        """
        record = allowlist_item_to_record(payload, version=now_ts())
        if not record["plate_number"]:
            return None
        record["updated_at"] = now_ts()
        self.allow_repo.upsert_records([record])
        return record_to_vehicle(record)

    def _lookup_online(self, plate: str) -> None:
        worker = LookupWorker(self.config, self.db.db_path, plate)
        worker.lookup_success.connect(self._on_lookup_success)
        worker.lookup_not_found.connect(self._on_lookup_not_found)
        worker.lookup_error.connect(self._on_lookup_error)
        worker.auth_expired.connect(self._handle_auth_required)
        worker.finished.connect(lambda: self._cleanup_worker(self._lookup_workers, worker))
        self._lookup_workers.append(worker)
        worker.start()

    def _on_lookup_success(self, data: dict) -> None:
        plate = normalize_plate(data.get("plate_number", ""))
        if not plate:
            return
        if not self._is_plate_on_screen(plate):
            return  # the guard has moved on to another vehicle
        vehicle = self._cache_vehicle_payload(data)
        self._evaluate_plate(plate, vehicle)

    def _on_lookup_not_found(self) -> None:
        """404 — the server has never seen this plate: unknown vehicle (orange)."""
        plate = normalize_plate(self.main_view.get_manual_inputs()[0])
        self.main_view.set_status_result("NOT FOUND (online)", level="warn")
        self.main_view.enable_not_found_actions(True)
        self._assessment = None
        if plate:
            self._evaluate_plate(plate, None)

    def _on_lookup_error(self, message: str) -> None:
        # Leave whatever the cache produced on screen — a failed lookup is not
        # evidence about the vehicle, only about the network.
        self.main_view.set_status_result(f"Lookup error: {message}", level="warn")
        self.main_view.enable_not_found_actions(True)

    def _is_plate_on_screen(self, plate: str) -> bool:
        return normalize_plate(self.main_view.get_manual_inputs()[0]) == normalize_plate(plate)

    # ------------------------------------------------------------------
    # On-the-spot vehicle registration (online only)
    # ------------------------------------------------------------------

    def _open_registration_dialog(self) -> None:
        plate = normalize_plate(self.main_view.get_manual_inputs()[0])
        state_plate = self._decision_state.plate if self._decision_state else ""
        plate = plate or state_plate

        if not (self.is_online and self._has_token()):
            QtWidgets.QMessageBox.warning(
                self,
                "Register Vehicle",
                "Registration requires a server connection.\n\n"
                "Nothing has been saved. Use 'Add Temporary Permit' for a local, "
                "offline allow instead.",
            )
            return

        cached = self.allow_repo.get_vehicle(plate) if plate else None
        if cached is not None and self._assess_vehicle(plate, cached).blacklisted:
            QtWidgets.QMessageBox.critical(
                self,
                "Register Vehicle",
                f"{plate} is BLACKLISTED and cannot be registered.",
            )
            return

        # The countdown must not fire while the guard is filling in the form.
        self._stop_countdown()

        dialog = RegistrationDialog(plate=plate, parent=self, prefill=cached)
        self._registration_dialog = dialog
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            self._registration_dialog = None
            return

        payload = dialog.payload()
        self._registration_dialog = None
        worker = RegisterVisitorWorker(self.config, self.db.db_path, payload)
        worker.register_success.connect(self._on_register_success)
        worker.register_conflict.connect(self._on_register_conflict)
        worker.register_offline.connect(self._on_register_offline)
        worker.register_error.connect(self._on_register_error)
        worker.auth_expired.connect(self._handle_auth_required)
        worker.finished.connect(lambda: self._cleanup_worker(self._register_workers, worker))
        self._register_workers.append(worker)
        worker.start()
        self.main_view.set_status_result("Registering vehicle…")

    def _on_register_success(self, data: dict) -> None:
        vehicle_payload = self._permit_body(data or {})
        plate = normalize_plate(vehicle_payload.get("plate_number", ""))
        if not plate:
            QtWidgets.QMessageBox.warning(
                self, "Register Vehicle", "The server returned an unexpected response."
            )
            return

        # Cache immediately so the very next detection of this plate goes GREEN
        # without waiting for the next allowlist sync.
        vehicle = self._cache_vehicle_payload(vehicle_payload)
        self.main_view.set_plate_text(plate)
        self._evaluate_plate(plate, vehicle)

        # Push the registration into the shared picture as well.
        if not self.sync_worker.isRunning():
            self.sync_worker.start()
        self.sync_worker.trigger_sync()

        valid_to = vehicle_payload.get("valid_to")
        until = f"\nValid until {self._format_ts(valid_to)}." if valid_to else ""
        QtWidgets.QMessageBox.information(
            self, "Register Vehicle", f"{plate} registered as a visitor.{until}"
        )

    def _on_register_conflict(self, message: str) -> None:
        """409 — someone just tried to register a blacklisted plate."""
        plate = normalize_plate(self.main_view.get_manual_inputs()[0])
        logger.warning("Registration refused — %s is blacklisted", plate)
        vehicle = self._cache_vehicle_payload({
            "plate_number": plate,
            "status": "BLACKLISTED",
            "alert": True,
        })
        self._evaluate_plate(plate, vehicle)   # straight into the red alarm state
        QtWidgets.QMessageBox.critical(
            self,
            "Blacklisted Vehicle",
            f"{plate} is BLACKLISTED and cannot be registered.\n\n"
            f"{message or 'The server refused the registration.'}",
        )

    def _on_register_offline(self, message: str) -> None:
        self.main_view.set_status_result("Registration failed (offline)", level="warn")
        QtWidgets.QMessageBox.warning(self, "Register Vehicle", message)

    def _on_register_error(self, message: str) -> None:
        self.main_view.set_status_result("Registration failed", level="warn")
        QtWidgets.QMessageBox.warning(self, "Register Vehicle", f"Failed: {message}")

    def _confirm_blacklist_override(
        self,
        assessment: PlateAssessment,
        decision: str,
        reason: str,
        note: str,
    ) -> bool:
        """Gate an ALLOW on a blacklisted plate. Returns True to proceed."""
        error = blacklist_override_error(assessment, decision, reason, note)
        if error:
            QtWidgets.QMessageBox.critical(self, "Blacklisted Vehicle", error)
            return False
        if not (assessment.blacklisted and decision == DECISION_ALLOW):
            return True

        answer = QtWidgets.QMessageBox.question(
            self,
            "Override Blacklist?",
            f"{assessment.plate} is BLACKLISTED.\n\n"
            f"Reason: {reason}\nNote: {note}\n\n"
            "This override will be recorded against your account. Allow entry?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return False
        logger.warning(
            "Blacklist override: plate=%s reason=%r note=%r", assessment.plate, reason, note
        )
        return True

    def _create_offline_temporary_allow(self, plate: str, reason: str, note: str) -> None:
        event_id = str(uuid.uuid4())
        ts = now_ts()
        user_profile = self.device_repo.get_user_profile()
        manual_by_user_id = user_profile.uuid if user_profile else None
        manual_by_username = user_profile.email if user_profile else None
        manual_reason_id = self.reason_repo.get_id_by_text(reason) if reason else None
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
            manual_by_user_id=manual_by_user_id,
            manual_by_username=manual_by_username,
            manual_reason_id=manual_reason_id,
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

    def _cleanup_worker(self, worker_list: list, worker) -> None:
        """Remove a finished worker from its tracking list."""
        if worker in worker_list:
            worker_list.remove(worker)

    def _has_token(self) -> bool:
        return token_store.has_token()

    @staticmethod
    def _format_ts(ts: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _restart_camera_service(self) -> None:
        self.camera_service.stop()
        self.camera_service = CameraService(
            mode=self.config.camera_mode,
            index=self.config.camera_index,
            rtsp_url=self.config.camera_rtsp_url,
        )
        self.camera_service.frame_ready.connect(self.main_view.update_frame)
        self.camera_service.status_changed.connect(self.main_view.set_camera_status)
        self.camera_service.plate_detected.connect(self._on_plate_detected)
        self.camera_service.start()


    def _restart_face_service(self) -> None:
        """Reopen the attendance webcam after a settings change.

        The worker captures its config at construction, so a changed
        FACE_CAMERA_INDEX only takes effect on a fresh one. Skipped entirely
        when attendance is off — there is no thread to restart.
        """
        if not self.attendance_enabled:
            return
        self._stop_face_service()
        self.face_service = FaceCameraService(self.config, self.db.db_path)
        self.face_service.frame_ready.connect(self.main_view.update_attendance_frame)
        self.face_service.status_changed.connect(
            self.main_view.set_attendance_camera_status
        )
        self.face_service.detection_changed.connect(self.main_view.set_face_detection)
        self.face_service.face_unrecognised.connect(self._on_face_unrecognised)
        self.face_service.punch_recorded.connect(self._on_punch_recorded)
        self.face_service.punch_suppressed.connect(self._on_punch_suppressed)
        if self._has_token():
            self.face_service.start()

    def _open_settings(self) -> None:
        self.settings_page.load_from_config(self.config)
        device = self.device_repo.get_device()
        self.settings_page.set_device_id(device.device_id if device else "")
        self.stack.setCurrentWidget(self.settings_page)

    def _on_settings_saved(self, config: AppConfig) -> None:
        auth_mode_changed = config.auth_mode != self.login_view.auth_mode
        # Turning attendance on or off changes which layout the main view was
        # built with, and rebuilding that under a live session would strand
        # every signal AppWindow has already connected. A restart is cheap; a
        # half-rewired screen in a guard booth is not.
        attendance_toggled = (
            bool(getattr(config, "face_attendance_enabled", False))
            != self.attendance_enabled
        )
        self.config = config
        self.api.config = config
        self.device = self.device_service.ensure_device(
            gate_id=config.gate_id,
            lane_id=config.lane_id,
            device_name=config.device_name,
        )
        self.main_view.set_gate_lane(config.gate_id, config.lane_id)
        self._restart_camera_service()
        self._restart_face_service()
        self.sync_worker.update_config(config)
        self.sync_worker.set_interval(config.sync_interval_seconds)
        # A changed countdown length only takes effect on the next vehicle.
        self._stop_countdown()
        self.countdown.set_seconds(config.auto_allow_seconds)
        self.login_view.set_portal_target(config.portal_sso_url, self.device.device_id)
        if auth_mode_changed:
            # The sign-in screen's widgets are built for one mode; swapping them
            # under a live session is not worth the state it would leave behind.
            QtWidgets.QMessageBox.information(
                self,
                "Restart Required",
                "The sign-in method changed. Restart Smart Gate for the new "
                "sign-in screen to take effect.",
            )
        if attendance_toggled:
            QtWidgets.QMessageBox.information(
                self,
                "Restart Required",
                "Staff attendance was turned "
                + ("on" if config.face_attendance_enabled else "off")
                + ". Restart Smart Gate for the new screen layout.",
            )
        self.stack.setCurrentWidget(self.main_view)

    def _handle_settings_cancelled(self) -> None:
        self.stack.setCurrentWidget(self.main_view)

    def _handle_auth_required(self) -> None:
        """Called when a token refresh fails — session expired, re-login needed."""
        if self.stack.currentWidget() is self.login_view:
            return  # already back at the login screen
        logger.warning("Session expired — forcing re-login")
        token_store.clear()
        self.device_repo.clear_session()
        self._reset_plate_state()
        self.is_online = False
        self.main_view.set_online_status(False)
        self.main_view.set_user("-")
        self.main_view.set_sync_status("Session expired")
        self.next_sync_at = None
        self._update_next_sync_label()
        if self.sync_worker.isRunning():
            self.sync_worker.stop()
            self.sync_worker.wait(2000)
        self.sync_worker = self._create_sync_worker()
        self._connect_sync_signals()
        self.camera_service.stop()
        self._stop_face_service()
        self.stack.setCurrentWidget(self.login_view)
        self.login_view.set_status("Session expired — please sign in again.")
        QtWidgets.QMessageBox.warning(
            self,
            "Session Expired",
            "Your session has expired. Please sign in again.",
        )

    def _handle_device_deprovisioned(self, message: str) -> None:
        """The portal deleted this device's record and revoked its sessions.

        Unlike an expired session this is not something signing in again fixes,
        so the message names the real cause and points at IT. The full logout
        path runs (including the best-effort ``/auth/logout``) so a retired or
        stolen machine keeps no usable credentials.
        """
        if self.stack.currentWidget() is self.login_view:
            return
        logger.warning("Device de-provisioned by the portal — signing out")
        self._handle_logout()
        self.main_view.set_sync_status("Device de-provisioned")
        self.login_view.set_status(message)
        QtWidgets.QMessageBox.critical(self, "Device De-provisioned", message)

    def _reset_plate_state(self) -> None:
        """Clear per-vehicle UI state so nothing leaks across sessions."""
        self._assessment = None
        self._last_ai_result = None
        self._clear_decision_state()
        self.main_view.set_offline_mode(False)
        self.main_view.clear_plate_detected()

    def _handle_logout(self) -> None:
        # The in-memory token goes immediately; the persisted refresh token is
        # dropped by the worker, which needs it first to revoke the session
        # server-side (portal mode). Either way the UI never waits on the network.
        token_store.clear()
        self._start_logout_worker()
        self._reset_plate_state()
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
        self._stop_face_service()
        self.stack.setCurrentWidget(self.login_view)

    def _start_logout_worker(self) -> None:
        if self.logout_worker is not None and self.logout_worker.isRunning():
            return
        self.logout_worker = LogoutWorker(self.config, self.db.db_path)
        self.logout_worker.start()

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
        # Timers first: the 5 s refresh tick fired after conn.close() once and
        # died on a closed database, taking the whole exit down with it.
        self.refresh_timer.stop()
        self.countdown_timer.stop()
        self._stop_countdown()
        self.alarm_service.stop()
        self.camera_service.stop()
        self._stop_face_service()
        # The speech thread is a daemon, but asking it to finish means a
        # half-spoken reminder is not cut off mid-word on the way out.
        try:
            self.speaker.stop()
        except Exception:
            logger.debug("Speaker stop failed", exc_info=True)
        self.sync_worker.stop()
        if not self.sync_worker.wait(4000):
            logger.warning("Sync worker did not stop in time on exit")
        # Short-lived request workers (lookup, permits, registration): letting
        # the window be destroyed while one still runs is the classic
        # "QThread: Destroyed while thread is still running" abort.
        for worker in (
            list(self._lookup_workers)
            + list(self._temp_permit_workers)
            + list(self._register_workers)
        ):
            if worker.isRunning():
                worker.wait(2000)
        if self.logout_worker is not None and self.logout_worker.isRunning():
            if not self.logout_worker.wait(2000):
                # The revoke is still in flight. Quitting must not leave the
                # persisted refresh token behind, so clear it here instead of
                # waiting out the request.
                self.device_repo.clear_session()
        self.conn.close()
        event.accept()


def main() -> None:
    setup_logging()
    config = load_config()

    # Force XCB (X11) backend on Linux — avoids Wayland/EGL failures that
    # break window management (move, resize) and camera rendering.
    import os
    if os.name != "nt":  # Linux / macOS
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    app = QtWidgets.QApplication([])
    # Fusion style gives consistent cross-platform widget rendering so that
    # QSS background-color on buttons works correctly on all Linux distros.
    app.setStyle("Fusion")

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
