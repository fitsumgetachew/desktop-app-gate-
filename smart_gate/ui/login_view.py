from __future__ import annotations

from urllib.parse import urlencode

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.services.auth_service import normalize_one_time_code
from smart_gate.ui.theme import get_logo_path, DAINTREE, LIGHT_BLUE, ORANGE, ORANGE_ALT, WHITE
from smart_gate.ui.widgets import CopyableField
from smart_gate.utils.config import AUTH_MODE_PORTAL


def build_sso_url(base_url: str, device_id: str) -> str:
    """``{PORTAL_SSO_URL}?client=smart-gate&device_id=…``, properly encoded.

    The code the portal mints is bound to ``device_id`` server-side, so this must
    carry the very same id the app registers with — a mismatch fails the exchange
    with a 401 that looks like a bad code. The id is emitted lowercase so the
    portal never has to reconcile two spellings of the same device.
    """
    query = urlencode({"client": "smart-gate", "device_id": (device_id or "").lower()})
    base = base_url if "?" in base_url else base_url.rstrip("/")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{query}"


class LoginView(QtWidgets.QWidget):
    """Branded login screen with centred card layout.

    Two sign-in modes share the card:

    * ``mock``   — email + password, the desktop drives both auth steps.
    * ``portal`` — the operator signs in on the SIT portal in a browser and
      pastes the one-time code; no credential is ever typed into this app.
    """

    login_requested = QtCore.Signal(str, str)   # mock mode: (email, password)
    code_submitted = QtCore.Signal(str)         # portal mode: one-time code

    def __init__(
        self,
        auth_mode: str = "mock",
        portal_sso_url: str = "",
        device_id: str = "",
    ) -> None:
        super().__init__()
        self.auth_mode = auth_mode
        self.portal_sso_url = portal_sso_url
        self.device_id = (device_id or "").lower()
        self.setStyleSheet(f"background-color: {LIGHT_BLUE};")

        # ── Card container ────────────────────────────────────────
        card = QtWidgets.QFrame()
        card.setObjectName("LoginCard")
        card.setFixedWidth(400)

        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(32, 36, 32, 32)
        card_layout.setSpacing(16)

        # Logo / fallback text
        logo_path = get_logo_path("light")  # white card background → primary color logo
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

        if self.auth_mode == AUTH_MODE_PORTAL:
            self._build_portal_form(card_layout)
        else:
            self._build_credentials_form(card_layout)

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

    # ── Form builders ─────────────────────────────────────────────

    def _primary_button(self, text: str) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(text)
        button.setObjectName("LoginBtn")
        button.setMinimumHeight(42)
        button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        button.setStyleSheet(
            f"QPushButton {{ background-color: {ORANGE}; color: {WHITE}; border: none;"
            f" font-weight: 600; font-size: 15px; border-radius: 6px; padding: 10px 0; }}"
            f"QPushButton:hover {{ background-color: {ORANGE_ALT}; }}"
            f"QPushButton:pressed {{ background-color: #D94D1F; }}"
        )
        return button

    def _build_credentials_form(self, card_layout: QtWidgets.QVBoxLayout) -> None:
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

        self.login_button = self._primary_button("Sign In")
        card_layout.addWidget(self.login_button)

        self.login_button.clicked.connect(self._on_login)
        self.password_input.returnPressed.connect(self._on_login)
        self.email_input.returnPressed.connect(lambda: self.password_input.setFocus())

    def _build_portal_form(self, card_layout: QtWidgets.QVBoxLayout) -> None:
        step1 = QtWidgets.QLabel("1. Sign in on the SIT portal")
        step1.setStyleSheet("font-weight: 500;")
        card_layout.addWidget(step1)

        self.portal_button = self._primary_button("Sign in via SIT Portal")
        card_layout.addWidget(self.portal_button)

        # A kiosk without a default browser must not be a dead end: the operator
        # can read the link off the screen and open it on a phone instead.
        hint = QtWidgets.QLabel("Can't open a browser? Open this on your phone:")
        hint.setStyleSheet("color: #6B7280; font-size: 11px;")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)

        self.sso_url_field = CopyableField(self.sso_url())
        card_layout.addWidget(self.sso_url_field)

        card_layout.addSpacing(4)

        # Provisioning needs this id typed into the portal. Show it here so it
        # can be copied rather than transcribed by eye.
        device_hint = QtWidgets.QLabel("Device ID (use this when provisioning in the portal):")
        device_hint.setStyleSheet("color: #6B7280; font-size: 11px;")
        device_hint.setWordWrap(True)
        card_layout.addWidget(device_hint)

        self.device_id_field = CopyableField(self.device_id)
        card_layout.addWidget(self.device_id_field)

        card_layout.addSpacing(4)

        step2 = QtWidgets.QLabel("2. Enter the code shown on the portal")
        step2.setStyleSheet("font-weight: 500;")
        card_layout.addWidget(step2)

        self.code_input = QtWidgets.QLineEdit()
        self.code_input.setPlaceholderText("abcd efgh ijkl")
        self.code_input.setMinimumHeight(38)
        card_layout.addWidget(self.code_input)

        self.continue_button = self._primary_button("Continue")
        card_layout.addWidget(self.continue_button)

        self.portal_button.clicked.connect(self._open_portal)
        self.continue_button.clicked.connect(self._on_continue)
        self.code_input.returnPressed.connect(self._on_continue)

    # ── Portal helpers ────────────────────────────────────────────

    def sso_url(self) -> str:
        return build_sso_url(self.portal_sso_url, self.device_id)

    def set_portal_target(self, portal_sso_url: str, device_id: str) -> None:
        """Update the SSO link after a config change (Settings save)."""
        self.portal_sso_url = portal_sso_url
        self.device_id = (device_id or "").lower()
        if hasattr(self, "sso_url_field"):
            self.sso_url_field.set_text(self.sso_url())
        if hasattr(self, "device_id_field"):
            self.device_id_field.set_text(self.device_id)

    def _open_portal(self) -> None:
        url = self.sso_url()
        # Never logged: only the base URL, so a code is never written to disk.
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            self.set_status(
                "Could not open a browser — open the link above on your phone."
            )

    # ── Public API ────────────────────────────────────────────────

    def _on_login(self) -> None:
        if not self.login_button.isEnabled():
            return  # a login is already in flight (Enter bypasses the disabled button)
        email = self.email_input.text().strip()
        password = self.password_input.text().strip()
        if not email or not password:
            self.set_status("Enter email and password")
            return
        self.login_requested.emit(email, password)

    def _on_continue(self) -> None:
        if not self.continue_button.isEnabled():
            return  # an exchange is already in flight
        code = normalize_one_time_code(self.code_input.text())
        if not code:
            self.set_status("Enter the code shown on the portal")
            return
        self.code_submitted.emit(code)

    def clear_credentials(self) -> None:
        """Drop whatever the operator typed once the session is established.

        A one-time code is dead the moment it is exchanged, so leaving it in the
        box only invites a retry that cannot work after the next logout.
        """
        if self.auth_mode == AUTH_MODE_PORTAL:
            self.code_input.clear()
        else:
            self.password_input.clear()

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def set_busy(self, busy: bool) -> None:
        """Lock the form while a LoginWorker is in flight.

        Without this, repeated clicks spawn concurrent workers that race each
        other writing tokens and the device row.
        """
        if self.auth_mode == AUTH_MODE_PORTAL:
            self.continue_button.setEnabled(not busy)
            self.continue_button.setText("Signing in..." if busy else "Continue")
            self.portal_button.setEnabled(not busy)
            # The code field stays editable so a rejected code can be corrected
            # without retyping it from scratch.
            self.code_input.setReadOnly(busy)
            return
        self.login_button.setEnabled(not busy)
        self.login_button.setText("Signing in..." if busy else "Sign In")
        self.email_input.setEnabled(not busy)
        self.password_input.setEnabled(not busy)
