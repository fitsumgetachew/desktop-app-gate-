"""Who this station can recognise, and who it cannot.

Opened from the attendance panel. Every row answers one question — can this
person be recognised at this gate right now — and, when the answer is no, says
which part is missing so it can be fixed in the portal rather than guessed at.
"""

from __future__ import annotations

from typing import Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.services.enrolment_status import (
    LEVEL_OK,
    LEVEL_WARN,
    EnrolmentSummary,
    StaffEnrolment,
    headline,
)
from smart_gate.ui.theme import (
    DAINTREE,
    STATE_GREEN,
    STATE_GREEN_SOFT,
    TEXT_MUTED,
    TEXT_SECONDARY,
    YELLOW,
    WHITE,
)

_LEVEL_COLORS = {
    LEVEL_OK: (STATE_GREEN_SOFT, STATE_GREEN),
    LEVEL_WARN: (YELLOW, DAINTREE),
}

COLUMNS = ["Staff", "Photos", "Embedded", "Plates", "Status"]


class StaffEnrolmentDialog(QtWidgets.QDialog):
    """A read-only view of the synced roster and its embeddings."""

    refresh_requested = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Staff Enrolment")
        self.setMinimumSize(680, 440)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Staff synced from the portal")
        title.setStyleSheet(f"font-size: 17px; font-weight: 700; color: {DAINTREE};")
        layout.addWidget(title)

        self.headline_label = QtWidgets.QLabel("")
        self.headline_label.setWordWrap(True)
        layout.addWidget(self.headline_label)

        self.counts_label = QtWidgets.QLabel("")
        self.counts_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 12px;"
        )
        layout.addWidget(self.counts_label)

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for column in range(1, len(COLUMNS) - 1):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.empty_label = QtWidgets.QLabel(
            "Nothing has been synced yet. Sign in and let one sync cycle finish."
        )
        self.empty_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        self.empty_label.hide()
        layout.addWidget(self.empty_label)

        buttons = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.setMinimumHeight(34)
        self.close_button = QtWidgets.QPushButton("Close")
        self.close_button.setMinimumHeight(34)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.close_button.clicked.connect(self.accept)

    # ------------------------------------------------------------------

    def set_enrolment(
        self, staff: Sequence[StaffEnrolment], summary: EnrolmentSummary
    ) -> None:
        text, level = headline(summary)
        background, foreground = _LEVEL_COLORS.get(level, (WHITE, TEXT_SECONDARY))
        self.headline_label.setText(text)
        self.headline_label.setStyleSheet(
            f"background-color: {background}; color: {foreground}; font-size: 13px;"
            " font-weight: 600; padding: 10px 12px; border-radius: 4px;"
        )
        self.counts_label.setText(
            f"{summary.staff_total} staff · {summary.photos_total} photos downloaded"
            f" · {summary.embedded_total} embeddings usable for recognition"
        )

        self.table.setRowCount(len(staff))
        for row, person in enumerate(staff):
            self._set_cell(row, 0, person.full_name)
            self._set_cell(row, 1, str(person.photo_count), centre=True)
            self._set_cell(row, 2, str(person.embedded_count), centre=True)
            self._set_cell(row, 3, str(person.plate_count), centre=True)
            self._set_cell(row, 4, person.status_text)
            if not person.recognisable:
                # The whole point of the table: make the unrecognisable ones
                # impossible to miss.
                for column in range(len(COLUMNS)):
                    item = self.table.item(row, column)
                    if item is not None:
                        item.setBackground(QtGui.QColor(YELLOW))
        self.table.setVisible(bool(staff))
        self.empty_label.setVisible(not staff)

    def _set_cell(self, row: int, column: int, text: str, centre: bool = False) -> None:
        item = QtWidgets.QTableWidgetItem(text)
        if centre:
            item.setTextAlignment(QtCore.Qt.AlignCenter)
        self.table.setItem(row, column, item)
