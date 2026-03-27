from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.ui.theme import DAINTREE, LIGHT_BLUE, ORANGE, ORANGE_ALT, WHITE
from smart_gate.utils.config import AppConfig, save_config


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

        sg_form.addRow("API Base URL", self.api_base_url)
        sg_form.addRow("Environment", self.env_mode)

        card_layout.addWidget(server_group)

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

        # ── Camera group ─────────────────────────────────────────
        cam_group = QtWidgets.QGroupBox("Camera")
        cg_form = QtWidgets.QFormLayout(cam_group)
        cg_form.setSpacing(10)
        cg_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.camera_mode = QtWidgets.QComboBox()
        self.camera_mode.addItems(["USB", "RTSP"])
        self.camera_mode.setMinimumHeight(34)
        self.camera_index = QtWidgets.QSpinBox()
        self.camera_index.setRange(0, 10)
        self.camera_index.setMinimumHeight(34)
        self.camera_rtsp_url = QtWidgets.QLineEdit()
        self.camera_rtsp_url.setMinimumHeight(34)

        cg_form.addRow("Mode", self.camera_mode)
        cg_form.addRow("Camera Index", self.camera_index)
        cg_form.addRow("RTSP URL", self.camera_rtsp_url)

        card_layout.addWidget(cam_group)

        # ── Storage / Sync group ─────────────────────────────────
        storage_group = QtWidgets.QGroupBox("Storage & Sync")
        stg_form = QtWidgets.QFormLayout(storage_group)
        stg_form.setSpacing(10)
        stg_form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.evidence_dir = QtWidgets.QLineEdit()
        self.evidence_dir.setMinimumHeight(34)
        self.sync_interval = QtWidgets.QSpinBox()
        self.sync_interval.setRange(5, 300)
        self.sync_interval.setMinimumHeight(34)
        self.sync_interval.setSuffix(" seconds")

        stg_form.addRow("Evidence Dir", self.evidence_dir)
        stg_form.addRow("Sync Interval", self.sync_interval)

        card_layout.addWidget(storage_group)

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

    # ==============================================================
    #  Public API (unchanged)
    # ==============================================================
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
