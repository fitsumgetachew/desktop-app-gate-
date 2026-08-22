"""Small shared widgets.

``CopyableField`` exists for one reason: the ``device_id`` is a UUID a human
otherwise retypes into the portal when provisioning a gate. A transposed or
truncated character produces a device that authenticates perfectly and never
matches its provisioning record — surfacing much later as permanent, silent
offline mode with nothing on screen naming the cause. Copying removes the
transcription step instead of trying to correct it afterwards.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

_VALUE_STYLE = (
    "color: #374151; font-family: monospace; font-size: 11px;"
    " background: #F3F4F6; border: 1px solid #E5E7EB;"
    " border-radius: 4px; padding: 6px;"
)


class CopyableField(QtWidgets.QWidget):
    """A selectable, wrapping value with a Copy button next to it."""

    def __init__(self, value: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.value_label = QtWidgets.QLabel(value)
        self.value_label.setWordWrap(True)
        self.value_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.value_label.setCursor(QtGui.QCursor(QtCore.Qt.IBeamCursor))
        self.value_label.setStyleSheet(_VALUE_STYLE)
        layout.addWidget(self.value_label, 1)

        self.copy_button = QtWidgets.QPushButton("Copy")
        self.copy_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.copy_button.setFixedWidth(64)
        self.copy_button.clicked.connect(self._copy)
        layout.addWidget(self.copy_button, 0, QtCore.Qt.AlignTop)

        self._reset_timer = QtCore.QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(lambda: self.copy_button.setText("Copy"))

    def text(self) -> str:
        return self.value_label.text()

    def set_text(self, value: str) -> None:
        self.value_label.setText(value)

    def _copy(self) -> None:
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is None:  # pragma: no cover - headless safety
            return
        clipboard.setText(self.value_label.text())
        self.copy_button.setText("Copied")
        self._reset_timer.start(1500)
