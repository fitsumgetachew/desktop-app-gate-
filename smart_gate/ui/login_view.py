from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.ui.theme import get_logo_path, DAINTREE, LIGHT_BLUE


class LoginView(QtWidgets.QWidget):
    """Branded login screen with centred card layout."""

    login_requested = QtCore.Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(f"background-color: {LIGHT_BLUE};")

        # ── Card container ────────────────────────────────────────
        card = QtWidgets.QFrame()
        card.setObjectName("LoginCard")
        card.setFixedWidth(400)

        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(32, 36, 32, 32)
        card_layout.setSpacing(16)

        # Logo / fallback text
        logo_path = get_logo_path("dark")
        self.logo_label = QtWidgets.QLabel()
        self.logo_label.setAlignment(QtCore.Qt.AlignCenter)
        if logo_path:
            pixmap = QtGui.QPixmap(logo_path)
            self.logo_label.setPixmap(
                pixmap.scaledToHeight(48, QtCore.Qt.SmoothTransformation)
            )
        else:
            self.logo_label.setText("SIT")
            self.logo_label.setStyleSheet(
                f"font-size: 28px; font-weight: 800; color: {DAINTREE}; "
                "letter-spacing: 4px;"
            )
        card_layout.addWidget(self.logo_label)

        # Title
        title = QtWidgets.QLabel("Smart Gate")
        title.setObjectName("LoginTitle")
        title.setAlignment(QtCore.Qt.AlignCenter)
        card_layout.addWidget(title)

        subtitle = QtWidgets.QLabel("Sign in to continue")
        subtitle.setObjectName("LoginSubtitle")
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        card_layout.addWidget(subtitle)

        card_layout.addSpacing(8)

        # Email
        email_label = QtWidgets.QLabel("Email")
        email_label.setStyleSheet("font-weight: 500;")
        self.email_input = QtWidgets.QLineEdit()
        self.email_input.setPlaceholderText("you@sit.edu")
        self.email_input.setMinimumHeight(38)
        card_layout.addWidget(email_label)
        card_layout.addWidget(self.email_input)

        # Password
        pw_label = QtWidgets.QLabel("Password")
        pw_label.setStyleSheet("font-weight: 500;")
        self.password_input = QtWidgets.QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_input.setMinimumHeight(38)
        card_layout.addWidget(pw_label)
        card_layout.addWidget(self.password_input)

        card_layout.addSpacing(4)

        # Login button
        self.login_button = QtWidgets.QPushButton("Sign In")
        self.login_button.setObjectName("LoginBtn")
        self.login_button.setMinimumHeight(42)
        self.login_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        card_layout.addWidget(self.login_button)

        # Status
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #D9534F; font-size: 12px;")
        card_layout.addWidget(self.status_label)

        # ── Outer layout – centres the card ───────────────────────
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        center_h = QtWidgets.QHBoxLayout()
        center_h.addStretch(1)
        center_h.addWidget(card)
        center_h.addStretch(1)

        outer.addStretch(1)
        outer.addLayout(center_h)
        outer.addStretch(1)

        # ── Signals ───────────────────────────────────────────────
        self.login_button.clicked.connect(self._on_login)
        self.password_input.returnPressed.connect(self._on_login)
        self.email_input.returnPressed.connect(lambda: self.password_input.setFocus())

    # ── Public API (unchanged) ────────────────────────────────────

    def _on_login(self) -> None:
        email = self.email_input.text().strip()
        password = self.password_input.text().strip()
        if not email or not password:
            self.set_status("Enter email and password")
            return
        self.login_requested.emit(email, password)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)
