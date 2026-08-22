"""On-the-spot visitor vehicle registration.

Online-only by design: a registration creates a record other gates and the
portal must see, so there is no offline fallback. When the server is
unreachable the guard uses the existing temporary-permit flow instead, which
is explicitly local and labelled as such.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.models.domain import VehicleRecord
from smart_gate.ui.theme import DAINTREE, ORANGE, ORANGE_ALT, TEXT_MUTED, WHITE
from smart_gate.utils.plates import normalize_plate
from smart_gate.utils.time import now_ts

DAY_SECONDS = 24 * 3600

# The server caps a visitor registration at 30 days.
VALIDITY_CHOICES = [
    ("1 day", 1 * DAY_SECONDS),
    ("3 days", 3 * DAY_SECONDS),
    ("7 days", 7 * DAY_SECONDS),
    ("30 days", 30 * DAY_SECONDS),
]


class RegistrationDialog(QtWidgets.QDialog):
    """Collects the visitor's details. Returns a payload, does not send it."""

    def __init__(
        self,
        plate: str = "",
        parent: Optional[QtWidgets.QWidget] = None,
        prefill: Optional[VehicleRecord] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Register Vehicle")
        self.setModal(True)
        self.setMinimumWidth(460)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        title = QtWidgets.QLabel("Register Vehicle")
        title.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {DAINTREE};")
        root.addWidget(title)

        # Spell out the cap: this is the long-lived path, and it is easy to
        # confuse with a temporary permit, which the server caps at 24 hours.
        subtitle = QtWidgets.QLabel(
            "Creates a visitor record on the server, valid for up to 30 days. "
            "Requires a connection."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        root.addWidget(subtitle)

        # ── Vehicle ──────────────────────────────────────────────────
        vehicle_group = QtWidgets.QGroupBox("Vehicle")
        vehicle_form = QtWidgets.QFormLayout(vehicle_group)
        vehicle_form.setSpacing(8)

        self.plate_input = QtWidgets.QLineEdit(normalize_plate(plate))
        self.plate_input.setPlaceholderText("ABC1234")
        self.plate_input.setMinimumHeight(34)
        self.vehicle_make = QtWidgets.QLineEdit()
        self.vehicle_make.setPlaceholderText("Toyota")
        self.vehicle_model = QtWidgets.QLineEdit()
        self.vehicle_model.setPlaceholderText("Corolla")
        self.vehicle_color = QtWidgets.QLineEdit()
        self.vehicle_color.setPlaceholderText("White")

        vehicle_form.addRow("Plate number *", self.plate_input)
        vehicle_form.addRow("Make", self.vehicle_make)
        vehicle_form.addRow("Model", self.vehicle_model)
        vehicle_form.addRow("Colour", self.vehicle_color)
        root.addWidget(vehicle_group)

        # ── Owner ────────────────────────────────────────────────────
        owner_group = QtWidgets.QGroupBox("Owner")
        owner_form = QtWidgets.QFormLayout(owner_group)
        owner_form.setSpacing(8)

        self.owner_first_name = QtWidgets.QLineEdit()
        self.owner_first_name.setPlaceholderText("First name")
        self.owner_last_name = QtWidgets.QLineEdit()
        self.owner_last_name.setPlaceholderText("Last name")
        self.phone = QtWidgets.QLineEdit()
        self.phone.setPlaceholderText("+251 …")

        owner_form.addRow("First name", self.owner_first_name)
        owner_form.addRow("Last name", self.owner_last_name)
        owner_form.addRow("Phone", self.phone)
        root.addWidget(owner_group)

        # ── Validity / note ──────────────────────────────────────────
        extra_group = QtWidgets.QGroupBox("Access")
        extra_form = QtWidgets.QFormLayout(extra_group)
        extra_form.setSpacing(8)

        self.validity = QtWidgets.QComboBox()
        for label, seconds in VALIDITY_CHOICES:
            self.validity.addItem(label, seconds)
        self.validity.setMinimumHeight(34)

        self.note = QtWidgets.QLineEdit()
        self.note.setPlaceholderText("Optional note (e.g. visiting Registrar)")

        extra_form.addRow("Valid for", self.validity)
        extra_form.addRow("Note", self.note)
        root.addWidget(extra_group)

        # ── Status line (errors from the caller land here) ───────────
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #D9534F; font-size: 12px;")
        self.status_label.hide()
        root.addWidget(self.status_label)

        # ── Buttons ──────────────────────────────────────────────────
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setMinimumHeight(36)
        self.cancel_button.setMinimumWidth(100)
        self.submit_button = QtWidgets.QPushButton("Register")
        self.submit_button.setMinimumHeight(36)
        self.submit_button.setMinimumWidth(130)
        self.submit_button.setDefault(True)
        self.submit_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.submit_button.setStyleSheet(
            f"QPushButton {{ background-color: {ORANGE}; color: {WHITE}; border: none;"
            f" font-weight: 600; border-radius: 6px; padding: 8px 18px; }}"
            f"QPushButton:hover {{ background-color: {ORANGE_ALT}; }}"
            f"QPushButton:disabled {{ background-color: #C8B6AF; }}"
        )
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.submit_button)
        root.addLayout(buttons)

        self.cancel_button.clicked.connect(self.reject)
        self.submit_button.clicked.connect(self._on_submit)

        if prefill is not None:
            self._apply_prefill(prefill)

    # ------------------------------------------------------------------

    def _apply_prefill(self, vehicle: VehicleRecord) -> None:
        """Seed the form from a known record (re-registering an expired permit)."""
        self.owner_first_name.setText(vehicle.owner_first_name or "")
        self.owner_last_name.setText(vehicle.owner_last_name or "")
        if not (vehicle.owner_first_name or vehicle.owner_last_name):
            self.owner_first_name.setText(vehicle.owner_name or "")
        self.phone.setText(vehicle.phone or "")
        self.vehicle_make.setText(vehicle.vehicle_make or "")
        self.vehicle_model.setText(vehicle.vehicle_model or "")
        self.vehicle_color.setText(vehicle.vehicle_color or "")
        self.note.setText(vehicle.note or "")

    def _on_submit(self) -> None:
        if not normalize_plate(self.plate_input.text()):
            self.set_error("A plate number is required.")
            self.plate_input.setFocus()
            return
        self.accept()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_error(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))

    def set_busy(self, busy: bool) -> None:
        self.submit_button.setEnabled(not busy)
        self.submit_button.setText("Registering..." if busy else "Register")

    def payload(self) -> Dict[str, Any]:
        """The POST /vehicles/register-visitor body. Empty fields are omitted.

        ``valid_to`` is an absolute epoch (the server caps it at 30 days out),
        so the chosen duration is converted here.
        """

        def text(widget: QtWidgets.QLineEdit) -> Optional[str]:
            value = widget.text().strip()
            return value or None

        body: Dict[str, Any] = {
            "plate_number": normalize_plate(self.plate_input.text()),
            "valid_to": now_ts() + int(self.validity.currentData()),
        }
        optional = {
            "owner_first_name": text(self.owner_first_name),
            "owner_last_name": text(self.owner_last_name),
            "phone": text(self.phone),
            "vehicle_make": text(self.vehicle_make),
            "vehicle_model": text(self.vehicle_model),
            "vehicle_color": text(self.vehicle_color),
            "note": text(self.note),
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return body
