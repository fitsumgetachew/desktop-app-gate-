from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.services.camera_discovery import DiscoveryWorker
from smart_gate.ui.theme import (
    BORDER,
    DAINTREE,
    LIGHT_BLUE,
    ORANGE,
    ORANGE_ALT,
    TEXT_MUTED,
    WHITE,
    YELLOW,
)
from smart_gate.ui.widgets import CopyableField
from smart_gate.utils.cameras import (
    KIND_RTSP,
    duplicate_device_roles,
    KIND_USB,
    KINDS,
    ROLE_FACE,
    ROLE_LABELS,
    ROLE_PLATE,
    ROLE_UNUSED,
    ROLES,
    CameraSource,
    new_camera_id,
)
from smart_gate.utils.environment import environment_label
from smart_gate.utils.config import (
    AUTH_MODES,
    DEFAULT_PORTAL_SSO_URL,
    AppConfig,
    save_config,
)


class CameraRow(QtWidgets.QFrame):
    """One configured camera: what it is, where it is, and what it is for.

    A row rather than a table cell because an RTSP URL and a USB index are
    different shapes of input, and swapping the editor as the type changes is
    far clearer than one field that means two things.
    """

    role_changed = QtCore.Signal(str, str)      # camera_id, role
    remove_requested = QtCore.Signal(str)       # camera_id

    def __init__(self, camera: CameraSource) -> None:
        super().__init__()
        self.camera_id = camera.id
        self.setObjectName("CameraRow")
        self.setStyleSheet(
            f"QFrame#CameraRow {{ border: 1px solid {BORDER}; border-radius: 6px;"
            f" background: {WHITE}; }}"
        )
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        self.name_input = QtWidgets.QLineEdit(camera.name)
        self.name_input.setPlaceholderText("Camera name")
        self.name_input.setMinimumHeight(30)

        self.role_combo = QtWidgets.QComboBox()
        for role in ROLES:
            self.role_combo.addItem(ROLE_LABELS[role], role)
        self.role_combo.setCurrentIndex(max(0, list(ROLES).index(camera.role)))
        self.role_combo.setMinimumHeight(30)
        self.role_combo.setToolTip(
            "What this camera is used for. Each job runs on one camera, so\n"
            "assigning a role here takes it away from whichever camera had it."
        )

        self.remove_button = QtWidgets.QToolButton()
        self.remove_button.setText("✕")
        self.remove_button.setToolTip("Remove this camera")
        self.remove_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.remove_button.setFixedSize(30, 30)

        self.kind_combo = QtWidgets.QComboBox()
        self.kind_combo.addItems(list(KINDS))
        self.kind_combo.setCurrentText(camera.kind)
        self.kind_combo.setMinimumHeight(30)
        self.kind_combo.setFixedWidth(90)

        self.index_spin = QtWidgets.QSpinBox()
        self.index_spin.setRange(0, 15)
        self.index_spin.setValue(camera.index)
        self.index_spin.setMinimumHeight(30)
        self.index_spin.setPrefix("device ")

        self.url_input = QtWidgets.QLineEdit(camera.url)
        self.url_input.setPlaceholderText(
            "rtsp://user:password@192.168.1.64:554/Streaming/Channels/102"
        )
        self.url_input.setMinimumHeight(30)

        layout.addWidget(self.name_input, 0, 0)
        layout.addWidget(self.role_combo, 0, 1)
        layout.addWidget(self.remove_button, 0, 2)
        layout.addWidget(self.kind_combo, 1, 0)
        layout.addWidget(self.index_spin, 1, 1, 1, 2)
        layout.addWidget(self.url_input, 1, 1, 1, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

        self.kind_combo.currentTextChanged.connect(self._apply_kind)
        self.role_combo.currentIndexChanged.connect(
            lambda: self.role_changed.emit(self.camera_id, self.role)
        )
        self.remove_button.clicked.connect(
            lambda: self.remove_requested.emit(self.camera_id)
        )
        self._apply_kind(camera.kind)

    def _apply_kind(self, kind: str) -> None:
        """Show the editor that matches the source type, never both."""
        rtsp = kind.upper() == KIND_RTSP
        self.url_input.setVisible(rtsp)
        self.index_spin.setVisible(not rtsp)

    @property
    def role(self) -> str:
        return self.role_combo.currentData() or ROLE_UNUSED

    def set_role_silently(self, role: str) -> None:
        """Change the role without re-emitting — used when another row takes it."""
        blocked = self.role_combo.blockSignals(True)
        self.role_combo.setCurrentIndex(max(0, list(ROLES).index(role)))
        self.role_combo.blockSignals(blocked)

    def to_camera(self) -> CameraSource:
        kind = self.kind_combo.currentText()
        return CameraSource(
            id=self.camera_id,
            name=self.name_input.text().strip() or "Camera",
            kind=kind,
            index=int(self.index_spin.value()),
            url=self.url_input.text().strip(),
            role=self.role,
        )


class SettingsPage(QtWidgets.QWidget):
    """Professional settings view with grouped form sections inside a
    scrollable card.  Functional logic (load / save) is unchanged.
    """

    settings_saved = QtCore.Signal(AppConfig)
    settings_cancelled = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("SettingsPage")
        self._config: AppConfig | None = None
        self.setStyleSheet(f"background-color: {LIGHT_BLUE};")

        # ── Card wrapper (centred, scrollable) ────────────────────
        card = QtWidgets.QFrame()
        card.setObjectName("SettingsCard")
        card.setMaximumWidth(600)

        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(20)

        # Title
        title = QtWidgets.QLabel("Settings")
        title.setStyleSheet(
            f"font-size: 20px; font-weight: 700; color: {DAINTREE}; padding-bottom: 4px;"
        )
        card_layout.addWidget(title)

        # ── Server group ─────────────────────────────────────────
        server_group = QtWidgets.QGroupBox("Server")
        sg_form = QtWidgets.QFormLayout(server_group)
        sg_form.setSpacing(10)
        sg_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.api_base_url = QtWidgets.QLineEdit()
        self.api_base_url.setMinimumHeight(34)
        self.env_mode = QtWidgets.QLineEdit()
        self.env_mode.setMinimumHeight(34)

        # Derived from the URL as it is typed, so the operator sees which
        # server (and therefore which local data set) a save would select.
        self.environment_hint = QtWidgets.QLabel("")
        self.environment_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.environment_hint.setWordWrap(True)
        self.api_base_url.textChanged.connect(self._refresh_environment_hint)

        sg_form.addRow("API Base URL", self.api_base_url)
        sg_form.addRow("", self.environment_hint)
        sg_form.addRow("Environment", self.env_mode)

        card_layout.addWidget(server_group)

        # ── Authentication group ─────────────────────────────────
        auth_group = QtWidgets.QGroupBox("Authentication")
        ag_form = QtWidgets.QFormLayout(auth_group)
        ag_form.setSpacing(10)
        ag_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.auth_mode = QtWidgets.QComboBox()
        self.auth_mode.addItems(list(AUTH_MODES))
        self.auth_mode.setMinimumHeight(34)
        self.auth_mode.setToolTip(
            "mock — sign in with email and password against the reference server.\n"
            "portal — the operator signs in on the SIT portal in a browser and\n"
            "pastes the one-time code. Takes effect after a restart."
        )
        self.portal_sso_url = QtWidgets.QLineEdit()
        self.portal_sso_url.setMinimumHeight(34)
        self.portal_sso_url.setToolTip(
            "Portal sign-in page. The app appends ?client=smart-gate&device_id=…"
        )

        # Read-only: the portal identifies this machine by this id, so it needs
        # to be copyable from wherever the operator happens to be looking.
        self.device_id_field = CopyableField("")

        ag_form.addRow("Sign-in mode", self.auth_mode)
        ag_form.addRow("Portal SSO URL", self.portal_sso_url)
        ag_form.addRow("Device ID", self.device_id_field)

        card_layout.addWidget(auth_group)

        # ── Gate group ───────────────────────────────────────────
        gate_group = QtWidgets.QGroupBox("Gate Configuration")
        gg_form = QtWidgets.QFormLayout(gate_group)
        gg_form.setSpacing(10)
        gg_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.gate_id = QtWidgets.QLineEdit()
        self.gate_id.setMinimumHeight(34)
        self.lane_id = QtWidgets.QLineEdit()
        self.lane_id.setMinimumHeight(34)
        self.direction = QtWidgets.QComboBox()
        self.direction.addItems(["ENTRY", "EXIT"])
        self.direction.setMinimumHeight(34)

        gg_form.addRow("Gate ID", self.gate_id)
        gg_form.addRow("Lane ID", self.lane_id)
        gg_form.addRow("Direction", self.direction)

        card_layout.addWidget(gate_group)

        # ── Cameras group ────────────────────────────────────────
        # A list rather than one fixed pair: a station may have a lane camera
        # that is not plugged in yet, a spare webcam, or eventually several
        # lanes. Each source says what it is for, so adding one never means
        # hand-editing a .env file on a gate PC.
        cam_group = QtWidgets.QGroupBox("Cameras")
        cam_box = QtWidgets.QVBoxLayout(cam_group)
        cam_box.setSpacing(10)

        hint = QtWidgets.QLabel(
            "Add each camera and choose what it is used for. Scanning finds USB "
            "cameras attached to this machine; IP cameras are added by URL."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        cam_box.addWidget(hint)

        button_row = QtWidgets.QHBoxLayout()
        button_row.setSpacing(8)
        self.scan_cameras_button = QtWidgets.QPushButton("Scan for cameras")
        self.scan_cameras_button.setMinimumHeight(32)
        self.scan_cameras_button.setCursor(
            QtGui.QCursor(QtCore.Qt.PointingHandCursor)
        )
        self.add_usb_camera_button = QtWidgets.QPushButton("Add USB")
        self.add_usb_camera_button.setMinimumHeight(32)
        self.add_usb_camera_button.setCursor(
            QtGui.QCursor(QtCore.Qt.PointingHandCursor)
        )
        self.add_rtsp_camera_button = QtWidgets.QPushButton("Add IP camera")
        self.add_rtsp_camera_button.setMinimumHeight(32)
        self.add_rtsp_camera_button.setCursor(
            QtGui.QCursor(QtCore.Qt.PointingHandCursor)
        )
        button_row.addWidget(self.scan_cameras_button)
        button_row.addWidget(self.add_usb_camera_button)
        button_row.addWidget(self.add_rtsp_camera_button)
        button_row.addStretch(1)
        cam_box.addLayout(button_row)

        self.camera_scan_status = QtWidgets.QLabel("")
        self.camera_scan_status.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px;"
        )
        self.camera_scan_status.hide()
        cam_box.addWidget(self.camera_scan_status)

        self.camera_rows_container = QtWidgets.QWidget()
        self.camera_rows_layout = QtWidgets.QVBoxLayout(self.camera_rows_container)
        self.camera_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.camera_rows_layout.setSpacing(8)
        cam_box.addWidget(self.camera_rows_container)

        # An unassigned role is not a harmless state: the services read the
        # flat camera_* fields, which keep their last value when nothing holds
        # the role. The gate therefore carries on using the previous camera
        # while this page says "Not used" — fail-safe, but silent, so say it.
        self.camera_role_warning = QtWidgets.QLabel("")
        self.camera_role_warning.setWordWrap(True)
        self.camera_role_warning.setStyleSheet(
            f"background-color: {YELLOW}; color: {DAINTREE}; font-size: 12px;"
            " font-weight: 600; padding: 8px 10px; border-radius: 4px;"
        )
        self.camera_role_warning.hide()
        cam_box.addWidget(self.camera_role_warning)

        self.no_cameras_label = QtWidgets.QLabel("No cameras configured yet.")
        self.no_cameras_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px; padding: 6px 0;"
        )
        cam_box.addWidget(self.no_cameras_label)

        card_layout.addWidget(cam_group)

        # ── Attendance group ─────────────────────────────────────
        att_group = QtWidgets.QGroupBox("Staff Attendance")
        att_form = QtWidgets.QFormLayout(att_group)
        att_form.setSpacing(10)
        att_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.face_attendance_enabled = QtWidgets.QCheckBox(
            "Recognise staff faces and record attendance"
        )
        self.face_attendance_enabled.setToolTip(
            "Turn off on a station with no webcam, or where the face libraries\n"
            "are not installed. The gate keeps working exactly as it does now."
        )
        self.face_max_fps = QtWidgets.QDoubleSpinBox()
        self.face_max_fps.setRange(0.5, 15.0)
        self.face_max_fps.setSingleStep(0.5)
        self.face_max_fps.setDecimals(1)
        self.face_max_fps.setMinimumHeight(34)
        self.face_max_fps.setSuffix(" per second")
        self.face_max_fps.setToolTip(
            "How often faces are checked. The plate pipeline shares this CPU;\n"
            "much above 3 and the two slow each other down."
        )
        self.face_tolerance = QtWidgets.QDoubleSpinBox()
        # 0.30-0.60, matching the loader's clamp. This box once allowed 0.10,
        # which rejects every real face on earth while the camera looks healthy
        # — recognition died silently until someone measured the distances.
        self.face_tolerance.setRange(0.30, 0.60)
        self.face_tolerance.setSingleStep(0.05)
        self.face_tolerance.setDecimals(2)
        self.face_tolerance.setMinimumHeight(34)
        self.face_tolerance.setToolTip(
            "Lower is stricter: fewer wrong matches, more missed ones.\n"
            "0.50 is what the university's running attendance system uses."
        )
        self.face_min_confidence = QtWidgets.QDoubleSpinBox()
        self.face_min_confidence.setRange(0.0, 100.0)
        self.face_min_confidence.setSingleStep(1.0)
        self.face_min_confidence.setDecimals(1)
        self.face_min_confidence.setMinimumHeight(34)
        self.face_min_confidence.setSuffix(" %")

        att_form.addRow("", self.face_attendance_enabled)
        att_form.addRow("Check rate", self.face_max_fps)
        att_form.addRow("Match tolerance", self.face_tolerance)
        att_form.addRow("Minimum confidence", self.face_min_confidence)

        card_layout.addWidget(att_group)

        # ── Storage / Sync group ─────────────────────────────────
        storage_group = QtWidgets.QGroupBox("Storage & Sync")
        stg_form = QtWidgets.QFormLayout(storage_group)
        stg_form.setSpacing(10)
        stg_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.alpr_roi = QtWidgets.QLineEdit()
        self.alpr_roi.setMinimumHeight(34)
        self.alpr_roi.setPlaceholderText("x,y,w,h as fractions — e.g. 0.55,0.25,0.45,0.55")
        self.alpr_roi.setToolTip(
            "Plate read zone: the AI reads only this part of the lane camera,\n"
            "at full resolution — a software zoom for a camera that sees the\n"
            "whole yard. The preview outlines the zone in orange. Four numbers,\n"
            "each 0-1: left offset, top offset, width, height.\n"
            "Empty = read the entire frame."
        )

        self.evidence_dir = QtWidgets.QLineEdit()
        self.evidence_dir.setMinimumHeight(34)
        self.sync_interval = QtWidgets.QSpinBox()
        self.sync_interval.setRange(5, 300)
        self.sync_interval.setMinimumHeight(34)
        self.sync_interval.setSuffix(" seconds")

        stg_form.addRow("Plate read zone", self.alpr_roi)
        stg_form.addRow("Evidence Dir", self.evidence_dir)
        stg_form.addRow("Sync Interval", self.sync_interval)

        card_layout.addWidget(storage_group)

        # ── Auto-decision group ──────────────────────────────────
        auto_group = QtWidgets.QGroupBox("Auto Decision")
        auto_form = QtWidgets.QFormLayout(auto_group)
        auto_form.setSpacing(10)
        auto_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.auto_allow_seconds = QtWidgets.QSpinBox()
        self.auto_allow_seconds.setRange(0, 60)
        self.auto_allow_seconds.setMinimumHeight(34)
        self.auto_allow_seconds.setSuffix(" seconds")
        self.auto_allow_seconds.setSpecialValueText("Disabled")
        self.auto_allow_seconds.setToolTip(
            "Countdown before a recognized, allowed vehicle is auto-approved.\n"
            "Set to 0 to require a manual decision every time."
        )

        auto_form.addRow("Auto-allow after", self.auto_allow_seconds)
        card_layout.addWidget(auto_group)

        # ── Action buttons ───────────────────────────────────────
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(12)
        btn_layout.addStretch(1)

        self.cancel_button = QtWidgets.QPushButton("Back")
        self.cancel_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.cancel_button.setMinimumWidth(100)
        self.cancel_button.setMinimumHeight(38)

        self.save_button = QtWidgets.QPushButton("Save Changes")
        self.save_button.setObjectName("PrimaryBtn")
        self.save_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.save_button.setMinimumWidth(140)
        self.save_button.setMinimumHeight(38)
        # Inline style has highest specificity — overrides any platform theme
        self.save_button.setStyleSheet(
            f"QPushButton {{ background-color: {ORANGE}; color: {WHITE}; border: none;"
            f" font-weight: 600; border-radius: 6px; padding: 8px 18px; }}"
            f"QPushButton:hover {{ background-color: {ORANGE_ALT}; }}"
            f"QPushButton:pressed {{ background-color: #D94D1F; }}"
        )

        btn_layout.addWidget(self.cancel_button)
        btn_layout.addWidget(self.save_button)

        card_layout.addLayout(btn_layout)

        # ── Scroll area to hold the card ─────────────────────────
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        scroll_inner = QtWidgets.QWidget()
        scroll_inner.setStyleSheet("background: transparent;")
        inner_layout = QtWidgets.QVBoxLayout(scroll_inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)

        center_h = QtWidgets.QHBoxLayout()
        center_h.addStretch(1)
        center_h.addWidget(card)
        center_h.addStretch(1)

        inner_layout.addSpacing(24)
        inner_layout.addLayout(center_h)
        inner_layout.addStretch(1)

        scroll.setWidget(scroll_inner)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # ── Signals ──────────────────────────────────────────────
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.settings_cancelled.emit)
        self.scan_cameras_button.clicked.connect(self._scan_cameras)
        self.add_usb_camera_button.clicked.connect(
            lambda: self._add_camera(KIND_USB)
        )
        self.add_rtsp_camera_button.clicked.connect(
            lambda: self._add_camera(KIND_RTSP)
        )
        self._camera_rows: list[CameraRow] = []
        self._scan_worker: DiscoveryWorker | None = None
        self._refresh_camera_placeholder()

    # ==============================================================
    #  Public API (unchanged)
    # ==============================================================
    def _refresh_environment_hint(self, text: str = "") -> None:
        label = environment_label(text or self.api_base_url.text())
        self.environment_hint.setText(
            f"Server: {label} — local data (cache, queues, roster, provisioning) "
            "is kept separately per server."
        )

    def set_device_id(self, device_id: str) -> None:
        """Show this machine's device id (not part of AppConfig — it lives in
        the device table, and the portal provisions against it)."""
        self.device_id_field.set_text((device_id or "").lower())

    def load_from_config(self, config: AppConfig) -> None:
        self._config = config
        self.api_base_url.setText(config.api_base_url)
        self._refresh_environment_hint(config.api_base_url)
        self.env_mode.setText(config.env_mode)
        self.auth_mode.setCurrentText(config.auth_mode)
        self.portal_sso_url.setText(config.portal_sso_url)
        self.gate_id.setText(config.gate_id)
        self.lane_id.setText(config.lane_id)
        self.direction.setCurrentText(config.direction)
        self._load_cameras(config.cameras)
        self.face_attendance_enabled.setChecked(config.face_attendance_enabled)
        self.face_max_fps.setValue(config.face_max_fps)
        self.face_tolerance.setValue(config.face_tolerance)
        self.face_min_confidence.setValue(config.face_min_confidence)
        self.alpr_roi.setText(config.alpr_roi)
        self.evidence_dir.setText(config.evidence_dir)
        self.sync_interval.setValue(config.sync_interval_seconds)
        self.auto_allow_seconds.setValue(config.auto_allow_seconds)

    def _save(self) -> None:
        if not self._config:
            return
        config = self._config
        config.api_base_url = self.api_base_url.text().strip()
        config.env_mode = self.env_mode.text().strip()
        config.auth_mode = self.auth_mode.currentText()
        config.portal_sso_url = (
            self.portal_sso_url.text().strip() or DEFAULT_PORTAL_SSO_URL
        )
        config.gate_id = self.gate_id.text().strip()
        config.lane_id = self.lane_id.text().strip()
        config.direction = self.direction.currentText()
        # The camera list is the source of truth; save_config derives the flat
        # camera_* fields the services actually read from whichever source
        # holds each role.
        cameras = [row.to_camera() for row in self._camera_rows]
        clash = duplicate_device_roles(cameras)
        if clash:
            # Saving this would look like a broken camera: the second pipeline
            # to open the device gets nothing and its preview just stays dark,
            # with no clue that the other role is holding it.
            role_a, role_b = clash
            QtWidgets.QMessageBox.warning(
                self,
                "One camera, two jobs",
                f"The same camera is assigned to both "
                f"{ROLE_LABELS.get(role_a, role_a)} and "
                f"{ROLE_LABELS.get(role_b, role_b)}.\n\n"
                "A camera can only be opened by one of them, so the other would "
                "show nothing at all. Give each job its own camera, or set one "
                "of them to Unused.",
            )
            return
        config.cameras = cameras
        config.face_attendance_enabled = self.face_attendance_enabled.isChecked()
        config.face_max_fps = float(self.face_max_fps.value())
        config.face_tolerance = float(self.face_tolerance.value())
        config.face_min_confidence = float(self.face_min_confidence.value())
        config.alpr_roi = self.alpr_roi.text().strip()
        config.evidence_dir = self.evidence_dir.text().strip()
        config.sync_interval_seconds = int(self.sync_interval.value())
        config.auto_allow_seconds = int(self.auto_allow_seconds.value())

        save_config(config)
        self.settings_saved.emit(config)

    # ==============================================================
    #  Camera list
    # ==============================================================

    def _load_cameras(self, cameras) -> None:
        """Rebuild the rows from config. Clears whatever was there before."""
        for row in list(self._camera_rows):
            self._remove_row(row)
        for camera in cameras or []:
            self._add_row(CameraSource(**camera.to_dict()))
        self._refresh_camera_placeholder()

    def _add_row(self, camera: CameraSource) -> CameraRow:
        row = CameraRow(camera)
        row.role_changed.connect(self._on_role_changed)
        row.remove_requested.connect(self._on_remove_requested)
        self.camera_rows_layout.addWidget(row)
        self._camera_rows.append(row)
        return row

    def _remove_row(self, row: CameraRow) -> None:
        if row in self._camera_rows:
            self._camera_rows.remove(row)
        self.camera_rows_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()

    def _on_remove_requested(self, camera_id: str) -> None:
        for row in list(self._camera_rows):
            if row.camera_id == camera_id:
                self._remove_row(row)
        self._refresh_camera_placeholder()

    def _on_role_changed(self, camera_id: str, role: str) -> None:
        """Roles are exclusive: the app runs one plate pipeline and one face
        pipeline, so two cameras claiming the same job would leave the choice to
        list order. Taking it off the previous holder makes that visible."""
        if role == ROLE_UNUSED:
            self._refresh_role_warning()
            return
        for row in self._camera_rows:
            if row.camera_id != camera_id and row.role == role:
                row.set_role_silently(ROLE_UNUSED)
        self._refresh_role_warning()

    def _next_free_index(self) -> int:
        used = {
            row.to_camera().index
            for row in self._camera_rows
            if row.to_camera().kind == KIND_USB
        }
        index = 0
        while index in used and index < 15:
            index += 1
        return index

    def _add_camera(self, kind: str) -> CameraRow:
        """Add an empty source of the given type, ready to be filled in."""
        is_rtsp = kind.upper() == KIND_RTSP
        camera = CameraSource(
            id=new_camera_id(),
            name="IP camera" if is_rtsp else "USB camera",
            kind=KIND_RTSP if is_rtsp else KIND_USB,
            index=0 if is_rtsp else self._next_free_index(),
            url="",
            role=self._suggest_role(),
        )
        row = self._add_row(camera)
        self._on_role_changed(camera.id, camera.role)
        self._refresh_camera_placeholder()
        return row

    def _suggest_role(self) -> str:
        """Offer the first job nothing is doing yet, else leave it unassigned."""
        taken = {row.role for row in self._camera_rows}
        for role in (ROLE_PLATE, ROLE_FACE):
            if role not in taken:
                return role
        return ROLE_UNUSED

    def _refresh_camera_placeholder(self) -> None:
        self.no_cameras_label.setVisible(not self._camera_rows)
        self._refresh_role_warning()

    def _refresh_role_warning(self) -> None:
        """Name any job that no camera is doing."""
        if not self._camera_rows:
            self.camera_role_warning.hide()
            return
        assigned = {row.role for row in self._camera_rows}
        missing = [
            ROLE_LABELS[role]
            for role in (ROLE_PLATE, ROLE_FACE)
            if role not in assigned
        ]
        if not missing:
            self.camera_role_warning.hide()
            return
        self.camera_role_warning.setText(
            "No camera is assigned to "
            + " or ".join(missing)
            + ". The app will keep using whichever camera it was already "
            "using for that, so this setting will appear to do nothing."
        )
        self.camera_role_warning.show()

    # ── Scanning ──────────────────────────────────────────────────

    def _scan_cameras(self) -> None:
        """Probe this machine for USB cameras, off the UI thread.

        Opening a camera can take seconds; doing it inline would freeze the
        settings window and read as a crash.
        """
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.scan_cameras_button.setEnabled(False)
        self._set_scan_status("Scanning for cameras…")
        worker = DiscoveryWorker()
        worker.finished_scan.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        worker.finished.connect(lambda: self.scan_cameras_button.setEnabled(True))
        self._scan_worker = worker
        worker.start()

    def _on_scan_finished(self, found) -> None:
        """Add anything new; never touch a camera the operator already set up."""
        existing = {
            row.to_camera().index
            for row in self._camera_rows
            if row.to_camera().kind == KIND_USB
        }
        added = 0
        for camera in found or []:
            if camera.index in existing:
                continue
            self._add_row(
                CameraSource(
                    id=new_camera_id(),
                    name=camera.label,
                    kind=KIND_USB,
                    index=camera.index,
                    url="",
                    role=self._suggest_role(),
                )
            )
            added += 1
        self._refresh_camera_placeholder()

        total = len(found or [])
        if total == 0:
            self._set_scan_status("No USB cameras found on this machine.")
        elif added == 0:
            self._set_scan_status(
                f"Found {total} camera(s) — all already configured."
            )
        else:
            self._set_scan_status(f"Found {total} camera(s), added {added} new.")

    def _on_scan_failed(self, reason: str) -> None:
        self._set_scan_status(f"Camera scan failed ({reason}).")

    def _set_scan_status(self, message: str) -> None:
        self.camera_scan_status.setText(message)
        self.camera_scan_status.setVisible(bool(message))
