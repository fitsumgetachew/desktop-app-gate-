from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from smart_gate.utils.config import AppConfig, save_config


class SettingsPage(QtWidgets.QWidget):
    settings_saved = QtCore.Signal(AppConfig)
    settings_cancelled = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("SettingsPage")
        self._config: AppConfig | None = None

        self.api_base_url = QtWidgets.QLineEdit()
        self.env_mode = QtWidgets.QLineEdit()
        self.gate_id = QtWidgets.QLineEdit()
        self.lane_id = QtWidgets.QLineEdit()
        self.direction = QtWidgets.QComboBox()
        self.direction.addItems(["ENTRY", "EXIT"])

        self.camera_mode = QtWidgets.QComboBox()
        self.camera_mode.addItems(["USB", "RTSP"])
        self.camera_index = QtWidgets.QSpinBox()
        self.camera_index.setRange(0, 10)
        self.camera_rtsp_url = QtWidgets.QLineEdit()

        self.evidence_dir = QtWidgets.QLineEdit()
        self.sync_interval = QtWidgets.QSpinBox()
        self.sync_interval.setRange(5, 300)

        form = QtWidgets.QFormLayout()
        form.addRow("API Base URL", self.api_base_url)
        form.addRow("Environment", self.env_mode)
        form.addRow("Gate ID", self.gate_id)
        form.addRow("Lane ID", self.lane_id)
        form.addRow("Direction", self.direction)
        form.addRow("Camera Mode", self.camera_mode)
        form.addRow("Camera Index", self.camera_index)
        form.addRow("Camera RTSP URL", self.camera_rtsp_url)
        form.addRow("Evidence Dir", self.evidence_dir)
        form.addRow("Sync Interval (s)", self.sync_interval)

        self.save_button = QtWidgets.QPushButton("Save")
        self.cancel_button = QtWidgets.QPushButton("Back")

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(QtWidgets.QLabel("Settings"))
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(button_layout)
        self.setLayout(layout)

        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.settings_cancelled.emit)

    def load_from_config(self, config: AppConfig) -> None:
        self._config = config
        self.api_base_url.setText(config.api_base_url)
        self.env_mode.setText(config.env_mode)
        self.gate_id.setText(config.gate_id)
        self.lane_id.setText(config.lane_id)
        self.direction.setCurrentText(config.direction)
        self.camera_mode.setCurrentText(config.camera_mode)
        self.camera_index.setValue(config.camera_index)
        self.camera_rtsp_url.setText(config.camera_rtsp_url)
        self.evidence_dir.setText(config.evidence_dir)
        self.sync_interval.setValue(config.sync_interval_seconds)

    def _save(self) -> None:
        if not self._config:
            return
        config = self._config
        config.api_base_url = self.api_base_url.text().strip()
        config.env_mode = self.env_mode.text().strip()
        config.gate_id = self.gate_id.text().strip()
        config.lane_id = self.lane_id.text().strip()
        config.direction = self.direction.currentText()
        config.camera_mode = self.camera_mode.currentText()
        config.camera_index = int(self.camera_index.value())
        config.camera_rtsp_url = self.camera_rtsp_url.text().strip()
        config.evidence_dir = self.evidence_dir.text().strip()
        config.sync_interval_seconds = int(self.sync_interval.value())

        save_config(config)
        self.settings_saved.emit(config)
