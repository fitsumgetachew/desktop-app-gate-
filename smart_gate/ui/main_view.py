from __future__ import annotations

from typing import List

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.ui.theme import (
    get_logo_path,
    DAINTREE,
    HALF_BAKED,
    SUCCESS,
    DANGER,
    TEXT_MUTED,
    WHITE,
)


class MainGateView(QtWidgets.QWidget):
    """Main operational view with SIT-branded header, QSplitter content, and
    polished controls.  All public signals and methods keep the same
    signature so ``AppWindow`` wiring is untouched.
    """

    # ── Signals (unchanged) ───────────────────────────────────────
    decision_requested = QtCore.Signal(str)
    capture_requested = QtCore.Signal()
    settings_requested = QtCore.Signal()
    logout_requested = QtCore.Signal()
    sync_now_requested = QtCore.Signal()
    check_status_requested = QtCore.Signal()
    sync_recheck_requested = QtCore.Signal()
    add_temp_permit_requested = QtCore.Signal()
    fullscreen_requested = QtCore.Signal()

    # ──────────────────────────────────────────────────────────────
    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 1. Header bar ────────────────────────────────────────
        root.addWidget(self._build_header())

        # ── 2. Sync strip ────────────────────────────────────────
        root.addWidget(self._build_sync_strip())

        # ── 3. Content (splitter) ────────────────────────────────
        root.addWidget(self._build_content(), 1)

        # ── Wire signals ─────────────────────────────────────────
        self._connect_buttons()
        self.enable_not_found_actions(False)

    # ==============================================================
    #  Header
    # ==============================================================
    def _build_header(self) -> QtWidgets.QFrame:
        bar = QtWidgets.QFrame()
        bar.setObjectName("HeaderBar")
        bar.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )

        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        # ── Left group: logo + title ─────────────────────────────
        logo_path = get_logo_path("light")
        self._logo_label = QtWidgets.QLabel()
        if logo_path:
            px = QtGui.QPixmap(logo_path)
            self._logo_label.setPixmap(
                px.scaledToHeight(30, QtCore.Qt.SmoothTransformation)
            )
        else:
            self._logo_label.setText("SIT")
            self._logo_label.setStyleSheet(
                f"font-size: 18px; font-weight: 800; color: {WHITE}; letter-spacing: 3px;"
            )
        layout.addWidget(self._logo_label)

        title = QtWidgets.QLabel("Smart Gate")
        title.setObjectName("HeaderTitle")
        layout.addWidget(title)

        layout.addSpacing(24)

        # ── Centre group: gate/lane, user, online badge ──────────
        self.gate_lane_label = QtWidgets.QLabel("Gate/Lane: -")
        self.user_label = QtWidgets.QLabel("User: -")
        self.online_status_label = QtWidgets.QLabel("Offline")
        self.online_status_label.setObjectName("HeaderBadgeOffline")

        for lbl in (self.gate_lane_label, self.user_label):
            lbl.setSizePolicy(
                QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred
            )
            layout.addWidget(lbl)

        self.online_status_label.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Preferred
        )
        layout.addWidget(self.online_status_label)

        # Flexible spacer pushes everything after it to the right
        layout.addStretch(1)

        # ── Right group: action buttons (always pinned right) ────
        self.sync_now_button = QtWidgets.QPushButton("Sync Now")
        self.sync_now_button.setObjectName("HeaderSyncBtn")
        self.sync_now_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.settings_button = QtWidgets.QPushButton("Settings")
        self.settings_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.fullscreen_button = QtWidgets.QPushButton("Fullscreen")
        self.fullscreen_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.logout_button = QtWidgets.QPushButton("Logout")
        self.logout_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        for btn in (self.sync_now_button, self.settings_button,
                    self.fullscreen_button, self.logout_button):
            btn.setSizePolicy(
                QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed
            )
            layout.addWidget(btn)

        return bar

    # ==============================================================
    #  Sync info strip
    # ==============================================================
    def _build_sync_strip(self) -> QtWidgets.QFrame:
        strip = QtWidgets.QFrame()
        strip.setObjectName("SyncStrip")

        layout = QtWidgets.QHBoxLayout(strip)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(20)

        self.sync_status_label = QtWidgets.QLabel("Sync: Idle")
        self.last_sync_label = QtWidgets.QLabel("Last sync: -")
        self.next_sync_label = QtWidgets.QLabel("Next sync in: -")
        self.camera_status_label = QtWidgets.QLabel("Camera: Disconnected")
        self.camera_status_label.setObjectName("CameraStatus")

        layout.addWidget(self.sync_status_label)
        layout.addWidget(self.last_sync_label)
        layout.addWidget(self.next_sync_label)
        layout.addStretch(1)
        layout.addWidget(self.camera_status_label)

        return strip

    # ==============================================================
    #  Content – splitter with left (camera+controls) / right (table)
    # ==============================================================
    def _build_content(self) -> QtWidgets.QSplitter:
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setHandleWidth(5)
        splitter.setChildrenCollapsible(False)
        splitter.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    # ── Left panel ────────────────────────────────────────────────
    def _build_left_panel(self) -> QtWidgets.QFrame:
        panel = QtWidgets.QFrame()
        panel.setObjectName("LeftPanel")

        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Camera preview
        self.camera_label = QtWidgets.QLabel("Camera")
        self.camera_label.setObjectName("CameraPreview")
        self.camera_label.setMinimumSize(320, 180)
        self.camera_label.setAlignment(QtCore.Qt.AlignCenter)
        self.camera_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        layout.addWidget(self.camera_label, 1)

        # ── Plate entry section ──────────────────────────────────
        plate_frame = QtWidgets.QFrame()
        plate_grid = QtWidgets.QGridLayout(plate_frame)
        plate_grid.setContentsMargins(0, 0, 0, 0)
        plate_grid.setSpacing(8)

        plate_lbl = QtWidgets.QLabel("Plate Number")
        plate_lbl.setStyleSheet("font-weight: 600;")
        self.plate_input = QtWidgets.QLineEdit()
        self.plate_input.setPlaceholderText("Enter plate number")
        self.plate_input.setMinimumHeight(36)

        self.check_status_button = QtWidgets.QPushButton("Check Status")
        self.check_status_button.setObjectName("PrimaryBtn")
        self.check_status_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.check_status_button.setMinimumHeight(36)

        self.check_online_checkbox = QtWidgets.QCheckBox("Check online too")

        self.status_result_label = QtWidgets.QLabel("Status: -")
        self.status_result_label.setStyleSheet("font-weight: 600;")
        self.presence_hint_label = QtWidgets.QLabel("Last state: -")
        self.presence_hint_label.setStyleSheet(f"color: {TEXT_MUTED};")

        self.sync_recheck_button = QtWidgets.QPushButton("Sync then re-check")
        self.sync_recheck_button.setObjectName("SecondaryBtn")
        self.sync_recheck_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.add_temp_permit_button = QtWidgets.QPushButton("Add Temporary Permit")
        self.add_temp_permit_button.setObjectName("SecondaryBtn")
        self.add_temp_permit_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        # Row 0
        plate_grid.addWidget(plate_lbl, 0, 0)
        plate_grid.addWidget(self.plate_input, 0, 1, 1, 2)
        plate_grid.addWidget(self.check_status_button, 0, 3)
        # Row 1
        plate_grid.addWidget(self.check_online_checkbox, 1, 1)
        plate_grid.addWidget(self.status_result_label, 1, 2, 1, 2)
        # Row 2
        plate_grid.addWidget(self.presence_hint_label, 2, 1, 1, 2)
        # Row 3
        plate_grid.addWidget(self.sync_recheck_button, 3, 0, 1, 2)
        plate_grid.addWidget(self.add_temp_permit_button, 3, 2, 1, 2)

        plate_grid.setColumnStretch(1, 1)
        plate_grid.setColumnStretch(2, 1)

        layout.addWidget(plate_frame)

        # ── Decision controls ────────────────────────────────────
        ctrl_frame = QtWidgets.QFrame()
        ctrl_grid = QtWidgets.QGridLayout(ctrl_frame)
        ctrl_grid.setContentsMargins(0, 0, 0, 0)
        ctrl_grid.setSpacing(8)

        reason_lbl = QtWidgets.QLabel("Reason")
        reason_lbl.setStyleSheet("font-weight: 600;")
        self.reason_dropdown = QtWidgets.QComboBox()
        self.reason_dropdown.addItem("Manual override")
        self.reason_dropdown.setMinimumHeight(34)

        note_lbl = QtWidgets.QLabel("Note")
        note_lbl.setStyleSheet("font-weight: 600;")
        self.note_input = QtWidgets.QLineEdit()
        self.note_input.setPlaceholderText("Optional note")
        self.note_input.setMinimumHeight(34)

        self.capture_button = QtWidgets.QPushButton("CAPTURE")
        self.capture_button.setObjectName("CaptureBtn")
        self.capture_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.capture_button.setMinimumHeight(38)

        self.allow_button = QtWidgets.QPushButton("ALLOW")
        self.allow_button.setObjectName("AllowBtn")
        self.allow_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.allow_button.setMinimumHeight(38)

        self.deny_button = QtWidgets.QPushButton("DENY")
        self.deny_button.setObjectName("DenyBtn")
        self.deny_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.deny_button.setMinimumHeight(38)

        ctrl_grid.addWidget(reason_lbl, 0, 0)
        ctrl_grid.addWidget(self.reason_dropdown, 0, 1, 1, 3)
        ctrl_grid.addWidget(note_lbl, 1, 0)
        ctrl_grid.addWidget(self.note_input, 1, 1, 1, 3)
        ctrl_grid.addWidget(self.capture_button, 2, 0)
        ctrl_grid.addWidget(self.allow_button, 2, 1)
        ctrl_grid.addWidget(self.deny_button, 2, 2)
        ctrl_grid.setColumnStretch(1, 1)
        ctrl_grid.setColumnStretch(2, 1)

        layout.addWidget(ctrl_frame)

        return panel

    # ── Right panel ───────────────────────────────────────────────
    def _build_right_panel(self) -> QtWidgets.QFrame:
        panel = QtWidgets.QFrame()
        panel.setObjectName("RightPanel")

        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QtWidgets.QLabel("Recent Events")
        header.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {DAINTREE}; padding-bottom: 4px;"
        )
        layout.addWidget(header)

        self.events_table = QtWidgets.QTableWidget(0, 5)
        self.events_table.setHorizontalHeaderLabels(
            ["Time", "Plate", "Decision", "Reason", "Synced"]
        )
        self.events_table.horizontalHeader().setStretchLastSection(True)
        self.events_table.setAlternatingRowColors(True)
        self.events_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.events_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.events_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setShowGrid(False)
        self.events_table.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        # Distribute column widths
        header_view = self.events_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header_view.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        header_view.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)

        layout.addWidget(self.events_table, 1)
        return panel

    # ==============================================================
    #  Signal wiring
    # ==============================================================
    def _connect_buttons(self) -> None:
        self.capture_button.clicked.connect(self.capture_requested.emit)
        self.allow_button.clicked.connect(lambda: self.decision_requested.emit("ALLOW"))
        self.deny_button.clicked.connect(lambda: self.decision_requested.emit("DENY"))
        self.settings_button.clicked.connect(self.settings_requested.emit)
        self.logout_button.clicked.connect(self.logout_requested.emit)
        self.sync_now_button.clicked.connect(self.sync_now_requested.emit)
        self.check_status_button.clicked.connect(self.check_status_requested.emit)
        self.sync_recheck_button.clicked.connect(self.sync_recheck_requested.emit)
        self.add_temp_permit_button.clicked.connect(self.add_temp_permit_requested.emit)
        self.fullscreen_button.clicked.connect(self.fullscreen_requested.emit)
        self.plate_input.returnPressed.connect(self.check_status_requested.emit)

    # ==============================================================
    #  Public API (signatures unchanged)
    # ==============================================================

    def update_frame(self, image: QtGui.QImage) -> None:
        pixmap = QtGui.QPixmap.fromImage(image)
        self.camera_label.setPixmap(
            pixmap.scaled(
                self.camera_label.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            )
        )

    def set_camera_status(self, connected: bool, message: str) -> None:
        status = "Connected" if connected else "Disconnected"
        self.camera_status_label.setText(f"Camera: {status} - {message}")

    def set_online_status(self, online: bool) -> None:
        if online:
            self.online_status_label.setText("Online")
            self.online_status_label.setObjectName("HeaderBadgeOnline")
        else:
            self.online_status_label.setText("Offline")
            self.online_status_label.setObjectName("HeaderBadgeOffline")
        # Force style refresh after object-name change
        self.online_status_label.style().unpolish(self.online_status_label)
        self.online_status_label.style().polish(self.online_status_label)

    def set_user(self, user: str) -> None:
        self.user_label.setText(f"User: {user}")

    def set_gate_lane(self, gate_id: str, lane_id: str) -> None:
        self.gate_lane_label.setText(f"Gate/Lane: {gate_id} / {lane_id}")

    def set_sync_status(self, message: str) -> None:
        self.sync_status_label.setText(f"Sync: {message}")

    def set_last_sync(self, message: str) -> None:
        self.last_sync_label.setText(f"Last sync: {message}")

    def set_next_sync(self, message: str) -> None:
        self.next_sync_label.setText(f"Next sync in: {message}")

    def set_reasons(self, reasons: List[str]) -> None:
        self.reason_dropdown.clear()
        for reason in reasons:
            self.reason_dropdown.addItem(reason)

    def get_manual_inputs(self):
        return (
            self.plate_input.text().strip(),
            self.reason_dropdown.currentText(),
            self.note_input.text().strip(),
        )

    def set_recent_events(self, rows) -> None:
        self.events_table.setRowCount(0)
        for row in rows:
            row_idx = self.events_table.rowCount()
            self.events_table.insertRow(row_idx)
            self.events_table.setItem(
                row_idx, 0, QtWidgets.QTableWidgetItem(str(row["event_time"]))
            )
            self.events_table.setItem(
                row_idx, 1, QtWidgets.QTableWidgetItem(row["plate_number_final"])
            )
            self.events_table.setItem(
                row_idx, 2, QtWidgets.QTableWidgetItem(row["decision"])
            )
            self.events_table.setItem(
                row_idx, 3, QtWidgets.QTableWidgetItem(row["manual_reason"] or "")
            )
            self.events_table.setItem(
                row_idx, 4, QtWidgets.QTableWidgetItem("Yes" if row["synced"] else "No")
            )

    def set_status_result(self, text: str) -> None:
        self.status_result_label.setText(f"Status: {text}")

    def set_presence_hint(self, text: str) -> None:
        self.presence_hint_label.setText(f"Last state: {text}")

    def set_plate_text(self, plate: str) -> None:
        self.plate_input.setText(plate)

    def is_check_online(self) -> bool:
        return self.check_online_checkbox.isChecked()

    def enable_not_found_actions(self, enabled: bool) -> None:
        self.sync_recheck_button.setEnabled(enabled)
        self.add_temp_permit_button.setEnabled(enabled)
