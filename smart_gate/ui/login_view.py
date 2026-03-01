from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class LoginView(QtWidgets.QWidget):
    login_requested = QtCore.Signal(str, str)

    def __init__(self) -> None:
        super().__init__()

        self.email_input = QtWidgets.QLineEdit()
        self.email_input.setPlaceholderText("Email")

        self.password_input = QtWidgets.QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QtWidgets.QLineEdit.Password)

        self.login_button = QtWidgets.QPushButton("Login")
        self.status_label = QtWidgets.QLabel("")

        form = QtWidgets.QFormLayout()
        form.addRow("Email", self.email_input)
        form.addRow("Password", self.password_input)

        layout = QtWidgets.QVBoxLayout()
        layout.addStretch(1)
        layout.addLayout(form)
        layout.addWidget(self.login_button)
        layout.addWidget(self.status_label)
        layout.addStretch(2)

        self.setLayout(layout)

        self.login_button.clicked.connect(self._on_login)

    def _on_login(self) -> None:
        email = self.email_input.text().strip()
        password = self.password_input.text().strip()
        if not email or not password:
            self.set_status("Enter email and password")
            return
        self.login_requested.emit(email, password)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)
