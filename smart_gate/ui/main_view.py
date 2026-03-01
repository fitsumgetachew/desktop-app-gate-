from __future__ import annotations

from typing import List

from PySide6 import QtCore, QtGui, QtWidgets


class MainGateView(QtWidgets.QWidget):
    decision_requested = QtCore.Signal(str)
    capture_requested = QtCore.Signal()
    settings_requested = QtCore.Signal()
    logout_requested = QtCore.Signal()
    sync_now_requested = QtCore.Signal()
    check_status_requested = QtCore.Signal()
    sync_recheck_requested = QtCore.Signal()
    add_temp_permit_requested = QtCore.Signal()
    fullscreen_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()

        self.camera_label = QtWidgets.QLabel("Camera")
        self.camera_label.setMinimumSize(640, 360)
        self.camera_label.setStyleSheet("background-color: #222; color: #fff;")
        self.camera_label.setAlignment(QtCore.Qt.AlignCenter)
        self.camera_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

        self.camera_status_label = QtWidgets.QLabel("Camera: Disconnected")
        self.online_status_label = QtWidgets.QLabel("Offline")
        self.user_label = QtWidgets.QLabel("User: -")
        self.gate_lane_label = QtWidgets.QLabel("Gate/Lane: -")

        self.sync_status_label = QtWidgets.QLabel("Sync: Idle")
        self.last_sync_label = QtWidgets.QLabel("Last sync: -")
        self.next_sync_label = QtWidgets.QLabel("Next sync in: -")

        self.plate_input = QtWidgets.QLineEdit()
        self.plate_input.setPlaceholderText("Enter plate number")

        self.check_online_checkbox = QtWidgets.QCheckBox("Check online too")

        self.check_status_button = QtWidgets.QPushButton("Check Status")
        self.sync_recheck_button = QtWidgets.QPushButton("Sync then re-check")
        self.add_temp_permit_button = QtWidgets.QPushButton("Add Temporary Permit")

        self.status_result_label = QtWidgets.QLabel("Status: -")
        self.presence_hint_label = QtWidgets.QLabel("Last state: -")

        self.reason_dropdown = QtWidgets.QComboBox()
        self.reason_dropdown.addItem("Manual override")

        self.note_input = QtWidgets.QLineEdit()
        self.note_input.setPlaceholderText("Optional note")

        self.capture_button = QtWidgets.QPushButton("CAPTURE")
        self.allow_button = QtWidgets.QPushButton("ALLOW")
        self.deny_button = QtWidgets.QPushButton("DENY")
        self.settings_button = QtWidgets.QPushButton("Settings")
        self.logout_button = QtWidgets.QPushButton("Logout")
        self.sync_now_button = QtWidgets.QPushButton("Sync Now")
        self.fullscreen_button = QtWidgets.QPushButton("Fullscreen")

        self.events_table = QtWidgets.QTableWidget(0, 5)
        self.events_table.setHorizontalHeaderLabels(
            ["Time", "Plate", "Decision", "Reason", "Synced"]
        )
        self.events_table.horizontalHeader().setStretchLastSection(True)
        self.events_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.events_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.events_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

        top_bar = QtWidgets.QHBoxLayout()
        top_bar.addWidget(self.online_status_label)
        top_bar.addSpacing(12)
        top_bar.addWidget(self.user_label)
        top_bar.addSpacing(12)
        top_bar.addWidget(self.gate_lane_label)
        top_bar.addStretch(1)
        top_bar.addWidget(self.sync_now_button)
        top_bar.addWidget(self.settings_button)
        top_bar.addWidget(self.fullscreen_button)
        top_bar.addWidget(self.logout_button)

        sync_bar = QtWidgets.QHBoxLayout()
        sync_bar.addWidget(self.sync_status_label)
        sync_bar.addStretch(1)
        sync_bar.addWidget(self.last_sync_label)
        sync_bar.addSpacing(8)
        sync_bar.addWidget(self.next_sync_label)

        status_layout = QtWidgets.QHBoxLayout()
        status_layout.addWidget(self.camera_status_label)
        status_layout.addStretch(1)

        plate_layout = QtWidgets.QGridLayout()
        plate_layout.addWidget(QtWidgets.QLabel("Plate"), 0, 0)
        plate_layout.addWidget(self.plate_input, 0, 1, 1, 3)
        plate_layout.addWidget(self.check_status_button, 0, 4)
        plate_layout.addWidget(self.check_online_checkbox, 1, 1)
        plate_layout.addWidget(self.status_result_label, 1, 2, 1, 3)
        plate_layout.addWidget(self.presence_hint_label, 2, 2, 1, 3)
        plate_layout.addWidget(self.sync_recheck_button, 2, 0, 1, 2)
        plate_layout.addWidget(self.add_temp_permit_button, 2, 4)

        controls_layout = QtWidgets.QGridLayout()
        controls_layout.addWidget(QtWidgets.QLabel("Reason"), 0, 0)
        controls_layout.addWidget(self.reason_dropdown, 0, 1, 1, 3)
        controls_layout.addWidget(QtWidgets.QLabel("Note"), 1, 0)
        controls_layout.addWidget(self.note_input, 1, 1, 1, 3)
        controls_layout.addWidget(self.capture_button, 2, 0)
        controls_layout.addWidget(self.allow_button, 2, 1)
        controls_layout.addWidget(self.deny_button, 2, 2)

        left_layout = QtWidgets.QVBoxLayout()
        left_layout.addLayout(status_layout)
        left_layout.addWidget(self.camera_label)
        left_layout.addLayout(plate_layout)
        left_layout.addLayout(controls_layout)
        left_layout.addStretch(1)

        right_layout = QtWidgets.QVBoxLayout()
        right_layout.addWidget(QtWidgets.QLabel("Recent Events"))
        right_layout.addWidget(self.events_table)

        left_widget = QtWidgets.QWidget()
        left_widget.setLayout(left_layout)
        right_widget = QtWidgets.QWidget()
        right_widget.setLayout(right_layout)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addLayout(top_bar)
        main_layout.addLayout(sync_bar)
        main_layout.addWidget(splitter, 1)

        self.setLayout(main_layout)

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

        self.enable_not_found_actions(False)

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
        self.online_status_label.setText("Online" if online else "Offline")

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
            self.events_table.setItem(row_idx, 0, QtWidgets.QTableWidgetItem(str(row["event_time"])))
            self.events_table.setItem(row_idx, 1, QtWidgets.QTableWidgetItem(row["plate_number_final"]))
            self.events_table.setItem(row_idx, 2, QtWidgets.QTableWidgetItem(row["decision"]))
            self.events_table.setItem(row_idx, 3, QtWidgets.QTableWidgetItem(row["manual_reason"] or ""))
            self.events_table.setItem(row_idx, 4, QtWidgets.QTableWidgetItem("Yes" if row["synced"] else "No"))

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
