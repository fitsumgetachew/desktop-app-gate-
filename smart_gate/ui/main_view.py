from __future__ import annotations

from typing import List

from PySide6 import QtCore, QtGui, QtWidgets

from smart_gate.services.attendance_display import (
    CONFIRMATION_HOLD_MS,
    LEVEL_IDLE,
    LEVEL_INFO,
    LEVEL_NEUTRAL,
    LEVEL_OK,
    AttendancePanelState,
    idle as attendance_idle,
    punch_count_text,
)
from smart_gate.services.decision_state import DecisionState, GateState, IDLE_STATE
from smart_gate.services.enrolment_status import (
    LEVEL_NEUTRAL as ENROL_NEUTRAL,
    LEVEL_OK as ENROL_OK,
    LEVEL_WARN as ENROL_WARN,
)
from smart_gate.services.face_overlay import (
    BOX_LINE_WIDTH,
    CORNER_LENGTH,
    CORNER_LINE_WIDTH,
    EMPTY_DETECTION,
    STATE_MATCHED,
    DetectionFrame,
    guide_rect,
    preview_scale,
)
from smart_gate.ui.theme import (
    get_logo_path,
    DAINTREE,
    HALF_BAKED,
    LIGHT_BLUE,
    ORANGE,
    SUCCESS,
    DANGER,
    TEXT_MUTED,
    TEXT_SECONDARY,
    WHITE,
    YELLOW,
    STATE_BORDER_WIDTH,
    STATE_GREEN,
    STATE_GREEN_SOFT,
    STATE_ORANGE,
    STATE_ORANGE_SOFT,
    STATE_RED,
    STATE_RED_SOFT,
)

# The role comes from the sign-in response (user.role). Shown for orientation —
# both roles may use every feature, so this never gates anything.
ROLE_LABELS = {
    "guard": "Gate Guard",
    "admin": "Administrator",
}

_OFFLINE_BANNER_STYLE = (
    f"background-color: {YELLOW}; color: {DAINTREE}; font-size: 12px;"
    " font-weight: 600; padding: 6px 16px;"
)

# Border + banner colour per traffic-light state. The border is deliberately
# thick: the guard reads it from across the booth, not from the desk.
_STATE_COLORS = {
    GateState.GREEN: (STATE_GREEN, STATE_GREEN_SOFT),
    GateState.RED: (STATE_RED, STATE_RED_SOFT),
    GateState.ORANGE: (STATE_ORANGE, STATE_ORANGE_SOFT),
}


# Attendance panel levels → colour. Deliberately parallel to _STATE_COLORS but
# a separate table: attendance must never borrow the gate's alarm red, and
# keeping them apart makes that structural rather than a matter of discipline.
_ATTENDANCE_LEVEL_COLORS = {
    LEVEL_OK: (STATE_GREEN, WHITE),
    LEVEL_INFO: (STATE_GREEN_SOFT, DAINTREE),
    LEVEL_NEUTRAL: (LIGHT_BLUE, TEXT_SECONDARY),
    LEVEL_IDLE: (LIGHT_BLUE, TEXT_MUTED),
}

# Tallest the lane preview may get inside the attendance-mode sidebar. Chosen
# so a 16:9 frame very nearly fills the column's width — a shorter cap wastes
# most of the panel on black bars — while ALLOW/DENY stay above the fold at the
# app's 900x560 minimum.
SIDEBAR_PREVIEW_MAX_HEIGHT = 480

# How long the "BARRIER OPEN SIGNAL" indicator stays lit.
BARRIER_FLASH_MS = 1800

# Detection runs at ~3 fps but frames arrive at ~30, so the last known boxes
# are drawn over the frames in between. After this long with no fresh
# detection the overlay clears itself — otherwise a box hangs in mid-air
# after someone walks away, or freezes on screen if the pipeline dies.
DETECTION_TTL_MS = 900

# Overlay colours: green once matched, SIT orange while still tracking.
_OVERLAY_MATCHED = STATE_GREEN
_OVERLAY_TRACKING = ORANGE

# Enrolment strip: how much of the roster this station can actually
# recognise. Amber is the important one — it is what says the portal sent
# staff but no photos, which otherwise only ever shows up as a camera that
# never recognises anybody.
_ENROLMENT_LEVEL_COLORS = {
    ENROL_OK: (STATE_GREEN_SOFT, DAINTREE),
    ENROL_WARN: (YELLOW, DAINTREE),
    ENROL_NEUTRAL: (LIGHT_BLUE, TEXT_SECONDARY),
}

_ATTENDANCE_NOTICE_STYLE = (
    f"background-color: {YELLOW}; color: {DAINTREE}; font-size: 14px;"
    " font-weight: 700; padding: 10px 14px; border-radius: 4px;"
)

_BARRIER_SIGNAL_STYLE = (
    f"background-color: {STATE_GREEN}; color: {WHITE}; font-size: 15px;"
    " font-weight: 800; letter-spacing: 1px; padding: 8px 14px;"
    " border-radius: 4px;"
)


def _attendance_state_style(background: str, foreground: str) -> str:
    return (
        f"background-color: {background}; color: {foreground}; font-size: 18px;"
        " font-weight: 700; padding: 14px 16px; border-radius: 6px;"
    )


def _camera_frame_style(color: str | None) -> str:
    """Border around the whole camera section for the current state."""
    if color is None:
        return "QFrame#CameraFrame { border: 1px solid #C4D4D1; border-radius: 8px; }"
    return (
        f"QFrame#CameraFrame {{ border: {STATE_BORDER_WIDTH} solid {color};"
        f" border-radius: 8px; }}"
    )


def _banner_style(color: str) -> str:
    return (
        f"background-color: {color}; color: {WHITE}; font-size: 17px;"
        " font-weight: 800; letter-spacing: 1px; padding: 10px 14px;"
        " border-radius: 4px;"
    )


def _subtext_style(soft: str, color: str) -> str:
    return (
        f"background-color: {soft}; color: {color}; font-size: 12px;"
        " font-weight: 600; padding: 6px 14px; border-radius: 4px;"
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
    # Traffic-light state actions
    auto_allow_cancelled = QtCore.Signal()      # STOP pressed during the green countdown
    alarm_acknowledged = QtCore.Signal()        # guard silenced the blacklist siren
    register_vehicle_requested = QtCore.Signal()  # orange state / toolbar action
    staff_details_requested = QtCore.Signal()    # open the enrolment breakdown

    # ──────────────────────────────────────────────────────────────
    def __init__(self, attendance_enabled: bool = False) -> None:
        super().__init__()
        # Defaults to False so every existing caller and test gets exactly
        # today's single-column gate screen.
        self._attendance_enabled = bool(attendance_enabled)
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

        # ── 3. Offline-mode banner ───────────────────────────────
        self.offline_banner = QtWidgets.QLabel("")
        self.offline_banner.setObjectName("OfflineBanner")
        self.offline_banner.setStyleSheet(_OFFLINE_BANNER_STYLE)
        self.offline_banner.setWordWrap(True)
        self.offline_banner.hide()
        root.addWidget(self.offline_banner)

        # ── 4. Content (splitter) ────────────────────────────────
        root.addWidget(self._build_content(), 1)

        # ── Wire signals ─────────────────────────────────────────
        self._connect_buttons()
        self.enable_not_found_actions(False)
        self._state = IDLE_STATE
        self.clear_decision_state()

        # ── Attendance panel state ───────────────────────────────
        # Single-shot timers so a confirmation returns to idle on its own and
        # the barrier indicator self-extinguishes; both are restarted rather
        # than stacked, so a burst of events cannot leave the panel stuck.
        self._attendance_reset_timer = QtCore.QTimer(self)
        self._attendance_reset_timer.setSingleShot(True)
        self._attendance_reset_timer.timeout.connect(self.clear_attendance_state)
        self._barrier_timer = QtCore.QTimer(self)
        self._barrier_timer.setSingleShot(True)
        # Latest face boxes, plus how long ago they arrived.
        self._detection: DetectionFrame = EMPTY_DETECTION
        self._detection_at = QtCore.QElapsedTimer()
        self._detection_at.start()
        if self._attendance_enabled:
            self._barrier_timer.timeout.connect(self.barrier_signal_label.hide)
            self.clear_attendance_state()
            self.set_attendance_count(0)
        else:
            self._barrier_timer.timeout.connect(self.barrier_signal_label.hide)

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
        logo_path = get_logo_path("dark")  # header bar is dark background → secondary color logo
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
        # Toolbar entry point for registering a manually-typed plate; the
        # orange state offers the same action for an ALPR-detected one.
        self.register_toolbar_button = QtWidgets.QPushButton("Register Vehicle")
        self.register_toolbar_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.sync_now_button = QtWidgets.QPushButton("Sync Now")
        self.sync_now_button.setObjectName("HeaderSyncBtn")
        self.sync_now_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.settings_button = QtWidgets.QPushButton("Settings")
        self.settings_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.fullscreen_button = QtWidgets.QPushButton("Fullscreen")
        self.fullscreen_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.logout_button = QtWidgets.QPushButton("Logout")
        self.logout_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        for btn in (self.register_toolbar_button, self.sync_now_button,
                    self.settings_button, self.fullscreen_button, self.logout_button):
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
    def _build_content(self) -> QtWidgets.QWidget:
        """The main area: today's gate layout, or attendance-main + gate sidebar.

        Both branches build the *same* left and right panels, once each — the
        gate flow is re-parented, never rebuilt, so every widget and every
        signal survives whichever layout is chosen.
        """
        left = self._build_left_panel()
        right = self._build_right_panel()

        if not self._attendance_enabled:
            # Byte-identical to the pre-attendance screen. This is what every
            # gate PC without a working face stack runs, so it is a first-class
            # layout, not a fallback with a hole in it.
            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            splitter.setHandleWidth(5)
            splitter.setChildrenCollapsible(False)
            splitter.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
            )
            splitter.addWidget(left)
            splitter.addWidget(right)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            self.gate_sidebar = None
            return splitter

        # Attendance is the main panel; the whole gate flow becomes a column.
        # Stacked vertically rather than side by side because the sidebar is now
        # narrow.
        #
        # The preview is still capped — left to grow it eats the column and
        # pushes ALLOW/DENY below the fold, which would make the guard scroll
        # for every single vehicle, the one interaction that must never get
        # slower. But with the events table gone from this column the cap can
        # be far more generous, and it needs to be: the label is as wide as the
        # sidebar, so a short one letterboxes the frame into a thin strip
        # between two black bars instead of showing the lane.
        self.camera_label.setMaximumHeight(SIDEBAR_PREVIEW_MAX_HEIGHT)
        # Expanding, not Preferred: Preferred parks the label at its 180px
        # minimum and hands the freed space to whatever sits below, which is
        # exactly how the frame ended up as a thin strip between black bars.
        # The cap above is what stops it going too far.
        self.camera_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )

        column = QtWidgets.QWidget()
        column_layout = QtWidgets.QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(8)
        # Spare height goes to the camera panel, not below it. The events table
        # used to absorb it, which left the preview parked at its 180px minimum
        # and letterboxed into a strip; with the table hidden here that space
        # would otherwise be handed to an almost-empty details box instead.
        column_layout.addWidget(left, 1)
        column_layout.addWidget(right, 0)

        # Last resort for a genuinely tiny window: with the preview capped the
        # column normally fits, and no scrollbar appears at all.
        sidebar = QtWidgets.QScrollArea()
        sidebar.setObjectName("GateSidebar")
        sidebar.setWidget(column)
        sidebar.setWidgetResizable(True)
        sidebar.setFrameShape(QtWidgets.QFrame.NoFrame)
        sidebar.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        sidebar.setMinimumWidth(360)
        self.gate_sidebar = sidebar

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setHandleWidth(5)
        splitter.setChildrenCollapsible(False)
        splitter.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        splitter.addWidget(self._build_attendance_panel())
        splitter.addWidget(sidebar)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    # ── Attendance panel (main) ───────────────────────────────────
    def _build_attendance_panel(self) -> QtWidgets.QFrame:
        panel = QtWidgets.QFrame()
        panel.setObjectName("AttendancePanel")
        panel.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Staff Attendance")
        title.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {DAINTREE};"
        )
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.attendance_camera_status_label = QtWidgets.QLabel("Camera: starting...")
        self.attendance_camera_status_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px;"
        )
        title_row.addWidget(self.attendance_camera_status_label)
        layout.addLayout(title_row)

        self.attendance_camera_label = QtWidgets.QLabel("Attendance camera starting...")
        self.attendance_camera_label.setAlignment(QtCore.Qt.AlignCenter)
        self.attendance_camera_label.setMinimumHeight(260)
        self.attendance_camera_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.attendance_camera_label.setStyleSheet(
            f"background-color: {DAINTREE}; color: {HALF_BAKED};"
            " border-radius: 8px; font-size: 13px;"
        )
        layout.addWidget(self.attendance_camera_label, 1)

        # The recognition result: the one thing a person walking up looks at.
        self.attendance_state_label = QtWidgets.QLabel("")
        self.attendance_state_label.setAlignment(QtCore.Qt.AlignCenter)
        self.attendance_state_label.setWordWrap(True)
        self.attendance_state_label.setMinimumHeight(58)
        layout.addWidget(self.attendance_state_label)

        # Car-without-attendance reminder, raised by the gate decision path.
        self.attendance_notice_banner = QtWidgets.QLabel("")
        self.attendance_notice_banner.setStyleSheet(_ATTENDANCE_NOTICE_STYLE)
        self.attendance_notice_banner.setWordWrap(True)
        self.attendance_notice_banner.setAlignment(QtCore.Qt.AlignCenter)
        self.attendance_notice_banner.hide()
        layout.addWidget(self.attendance_notice_banner)

        self.attendance_count_label = QtWidgets.QLabel("")
        self.attendance_count_label.setAlignment(QtCore.Qt.AlignCenter)
        self.attendance_count_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 13px; font-weight: 600;"
        )
        layout.addWidget(self.attendance_count_label)

        # ── Enrolment strip ──────────────────────────────────────
        # Always visible, because "how many staff can this station actually
        # recognise" is the question behind every "Not recognised", and the
        # answer lives in the portal rather than on this machine.
        enrol_row = QtWidgets.QHBoxLayout()
        enrol_row.setSpacing(8)
        self.enrolment_label = QtWidgets.QLabel("")
        self.enrolment_label.setWordWrap(True)
        self.enrolment_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
        )
        self.staff_details_button = QtWidgets.QPushButton("Staff…")
        self.staff_details_button.setMinimumHeight(30)
        self.staff_details_button.setCursor(
            QtGui.QCursor(QtCore.Qt.PointingHandCursor)
        )
        self.staff_details_button.setToolTip(
            "Which staff have been synced, and how many of their photos this "
            "station could actually use."
        )
        enrol_row.addWidget(self.enrolment_label, 1)
        enrol_row.addWidget(self.staff_details_button)
        layout.addLayout(enrol_row)
        return panel

    # ── Camera section (video + traffic-light state) ──────────────
    def _build_camera_section(self) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("CameraFrame")
        frame.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.camera_frame = frame

        box = QtWidgets.QVBoxLayout(frame)
        box.setContentsMargins(6, 6, 6, 6)
        box.setSpacing(6)

        self.camera_label = QtWidgets.QLabel("Camera")
        self.camera_label.setObjectName("CameraPreview")
        self.camera_label.setMinimumSize(320, 180)
        self.camera_label.setAlignment(QtCore.Qt.AlignCenter)
        self.camera_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        box.addWidget(self.camera_label, 1)

        # Headline: "✓ ABC1234 — Abebe Bekele (STAFF)" / "⛔ BLACKLISTED …"
        self.decision_banner = QtWidgets.QLabel("")
        self.decision_banner.setObjectName("DecisionBanner")
        self.decision_banner.setAlignment(QtCore.Qt.AlignCenter)
        self.decision_banner.setWordWrap(True)
        self.decision_banner.hide()
        box.addWidget(self.decision_banner)

        # Secondary line: countdown, expiry reason, owner summary.
        self.decision_subtext = QtWidgets.QLabel("")
        self.decision_subtext.setObjectName("DecisionSubtext")
        self.decision_subtext.setAlignment(QtCore.Qt.AlignCenter)
        self.decision_subtext.setWordWrap(True)
        self.decision_subtext.hide()
        box.addWidget(self.decision_subtext)

        # State actions — only the one relevant to the current state is shown.
        self.state_actions = QtWidgets.QWidget()
        actions = QtWidgets.QHBoxLayout(self.state_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)

        self.stop_auto_button = QtWidgets.QPushButton("■  STOP — do not open")
        self.stop_auto_button.setMinimumHeight(42)
        self.stop_auto_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.stop_auto_button.setStyleSheet(
            f"QPushButton {{ background-color: {DAINTREE}; color: {WHITE}; border: none;"
            " font-size: 14px; font-weight: 800; border-radius: 6px; padding: 8px 26px; }"
            f"QPushButton:hover {{ background-color: {STATE_RED}; }}"
        )
        self.stop_auto_button.hide()
        actions.addWidget(self.stop_auto_button)

        self.ack_alarm_button = QtWidgets.QPushButton("Acknowledge alarm")
        self.ack_alarm_button.setMinimumHeight(42)
        self.ack_alarm_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.ack_alarm_button.setStyleSheet(
            f"QPushButton {{ background-color: {WHITE}; color: {STATE_RED};"
            f" border: 2px solid {STATE_RED}; font-size: 14px; font-weight: 700;"
            " border-radius: 6px; padding: 8px 26px; }"
            f"QPushButton:hover {{ background-color: {STATE_RED_SOFT}; }}"
        )
        self.ack_alarm_button.hide()
        actions.addWidget(self.ack_alarm_button)

        self.register_vehicle_button = QtWidgets.QPushButton("Register vehicle")
        self.register_vehicle_button.setMinimumHeight(42)
        self.register_vehicle_button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.register_vehicle_button.setStyleSheet(
            f"QPushButton {{ background-color: {STATE_ORANGE}; color: {WHITE}; border: none;"
            " font-size: 14px; font-weight: 700; border-radius: 6px; padding: 8px 26px; }"
            "QPushButton:hover { background-color: #F15A24; }"
        )
        self.register_vehicle_button.hide()
        actions.addWidget(self.register_vehicle_button)

        actions.addStretch(1)
        self.state_actions.hide()
        box.addWidget(self.state_actions)

        return frame

    # ── Left panel ────────────────────────────────────────────────
    def _build_left_panel(self) -> QtWidgets.QFrame:
        panel = QtWidgets.QFrame()
        panel.setObjectName("LeftPanel")

        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Camera section: video + traffic-light banner + state actions, all
        # inside one frame so the state border wraps the whole thing.
        layout.addWidget(self._build_camera_section(), 1)

        # ── Barrier open signal ──────────────────────────────────
        # Visual only, and deliberately below the traffic light rather than
        # inside it: this reports what the app *signalled*, not what the gate
        # decided, and the two must not be mistaken for one another.
        self.barrier_signal_label = QtWidgets.QLabel("BARRIER OPEN SIGNAL")
        self.barrier_signal_label.setObjectName("BarrierSignal")
        self.barrier_signal_label.setAlignment(QtCore.Qt.AlignCenter)
        self.barrier_signal_label.setStyleSheet(_BARRIER_SIGNAL_STYLE)
        self.barrier_signal_label.hide()
        layout.addWidget(self.barrier_signal_label)

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

        self.ai_confidence_label = QtWidgets.QLabel("")
        self.ai_confidence_label.setStyleSheet(f"font-size: 11px; color: {TEXT_MUTED};")

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
        plate_grid.addWidget(self.ai_confidence_label, 1, 0)
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

        # ── Vehicle details ──────────────────────────────────────
        self.vehicle_group = QtWidgets.QGroupBox("Vehicle Details")
        details_box = QtWidgets.QVBoxLayout(self.vehicle_group)
        details_box.setContentsMargins(12, 8, 12, 10)
        details_box.setSpacing(4)

        self.vehicle_details_form = QtWidgets.QFormLayout()
        self.vehicle_details_form.setSpacing(4)
        self.vehicle_details_form.setLabelAlignment(QtCore.Qt.AlignRight)
        self.vehicle_details_form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow
        )
        details_box.addLayout(self.vehicle_details_form)

        self.vehicle_details_empty = QtWidgets.QLabel("No vehicle selected.")
        self.vehicle_details_empty.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        details_box.addWidget(self.vehicle_details_empty)

        layout.addWidget(self.vehicle_group)

        header = QtWidgets.QLabel("Recent Events")
        header.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {DAINTREE}; padding-bottom: 4px;"
        )
        self.events_header = header

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

        layout.addWidget(self.events_header)
        layout.addWidget(self.events_table, 1)

        if self._attendance_enabled:
            # The gate is a narrow sidebar here and the live lane view is what
            # the guard actually reads a plate from — an event log they can
            # check afterwards is not worth the vertical space it costs the
            # camera. Hidden rather than skipped so set_recent_events() keeps
            # working and the history is still there when the sidebar is not.
            self.events_header.hide()
            self.events_table.hide()

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
        self.stop_auto_button.clicked.connect(self.auto_allow_cancelled.emit)
        self.ack_alarm_button.clicked.connect(self.alarm_acknowledged.emit)
        self.register_vehicle_button.clicked.connect(self.register_vehicle_requested.emit)
        self.register_toolbar_button.clicked.connect(self.register_vehicle_requested.emit)
        self.plate_input.returnPressed.connect(self.check_status_requested.emit)
        if self._attendance_enabled:
            self.staff_details_button.clicked.connect(
                self.staff_details_requested.emit
            )

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

    def set_user(self, user: str, role: str = "") -> None:
        """Show who is signed in, and as what.

        Orientation only — both roles may use every feature the app has, so
        nothing is hidden on the strength of this label.
        """
        label = ROLE_LABELS.get((role or "").lower())
        if not label and role:
            label = role.replace("_", " ").title()
        self.user_label.setText(f"User: {user}" + (f" ({label})" if label else ""))

    def set_gate_lane(
        self,
        gate_id: str,
        lane_id: str,
        gate_name: str | None = None,
        lane_name: str | None = None,
    ) -> None:
        """Show the gate/lane this machine is provisioned for.

        The server's display names are preferred when it sends them: an operator
        can check "Main Gate / Entry Lane" against the lane they are standing at
        far more reliably than "GATE-1 / LANE-A".
        """
        gate = f"{gate_name} ({gate_id})" if gate_name and gate_name != gate_id else gate_id
        lane = f"{lane_name} ({lane_id})" if lane_name and lane_name != lane_id else lane_id
        self.gate_lane_label.setText(f"Gate/Lane: {gate} / {lane}")

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

    def set_status_result(self, text: str, level: str = "normal") -> None:
        """Show the resolved plate status.

        ``level`` is one of ``normal``, ``warn`` (e.g. EXPIRED / NOT FOUND) or
        ``alarm`` (BLACKLISTED).
        """
        self.status_result_label.setText(f"Status: {text}")
        if level == "alarm":
            style = f"font-weight: 800; color: {DANGER};"
        elif level == "warn":
            style = f"font-weight: 700; color: {ORANGE};"
        else:
            style = "font-weight: 600;"
        self.status_result_label.setStyleSheet(style)

    # ==============================================================
    #  Traffic-light decision state
    # ==============================================================

    def set_decision_state(self, state: DecisionState) -> None:
        """Drive the camera section into GREEN / RED / ORANGE.

        Owns the border, the banner and which state action is offered. The
        countdown text is written separately by :meth:`set_countdown` so a
        per-second tick does not rebuild the whole banner.
        """
        self._state = state
        if state.is_idle:
            self.clear_decision_state()
            return

        color, soft = _STATE_COLORS[state.state]
        self.camera_frame.setStyleSheet(_camera_frame_style(color))

        self.decision_banner.setText(state.headline)
        self.decision_banner.setStyleSheet(_banner_style(color))
        self.decision_banner.show()

        self.decision_subtext.setText(state.subtext)
        self.decision_subtext.setStyleSheet(_subtext_style(soft, color))
        self.decision_subtext.setVisible(bool(state.subtext))

        self.stop_auto_button.setVisible(False)   # shown by set_countdown
        self.ack_alarm_button.setVisible(state.alarm)
        self.register_vehicle_button.setVisible(state.can_register)
        self.state_actions.setVisible(state.alarm or state.can_register)

        # Pre-select the most likely action so Enter/Space does the right thing.
        if state.state is GateState.RED:
            self.deny_button.setDefault(True)
            self.allow_button.setDefault(False)
        elif state.state is GateState.GREEN:
            self.allow_button.setDefault(True)
            self.deny_button.setDefault(False)
        else:
            self.allow_button.setDefault(False)
            self.deny_button.setDefault(False)

        self.set_vehicle_details(state.details)

    def set_countdown(self, remaining: int) -> None:
        """Show 'opening in N s' plus the STOP button, or hide both at 0."""
        if remaining <= 0:
            self.stop_auto_button.hide()
            self.state_actions.setVisible(
                self.ack_alarm_button.isVisible() or self.register_vehicle_button.isVisible()
            )
            if self._state.state is GateState.GREEN:
                self.decision_subtext.setText(self._state.subtext)
            return

        color, soft = _STATE_COLORS[GateState.GREEN]
        plural = "" if remaining == 1 else "s"
        self.decision_subtext.setText(f"Opening in {remaining} second{plural}…")
        self.decision_subtext.setStyleSheet(_subtext_style(soft, color))
        self.decision_subtext.show()
        self.stop_auto_button.show()
        self.state_actions.show()

    def clear_decision_state(self) -> None:
        """Back to neutral: no border, no banner, no state actions."""
        self._state = IDLE_STATE
        self.camera_frame.setStyleSheet(_camera_frame_style(None))
        self.decision_banner.clear()
        self.decision_banner.hide()
        self.decision_subtext.clear()
        self.decision_subtext.hide()
        self.stop_auto_button.hide()
        self.ack_alarm_button.hide()
        self.register_vehicle_button.hide()
        self.state_actions.hide()
        self.allow_button.setDefault(False)
        self.deny_button.setDefault(False)
        self.set_vehicle_details([])

    def set_alarm_acknowledged(self) -> None:
        """Hide the acknowledge button once the guard has silenced the siren."""
        self.ack_alarm_button.hide()
        self.state_actions.setVisible(self.register_vehicle_button.isVisible())

    def set_vehicle_details(self, rows) -> None:
        """Render (label, value) rows; an empty list collapses the panel body."""
        while self.vehicle_details_form.rowCount():
            self.vehicle_details_form.removeRow(0)

        rows = list(rows or [])
        for label, value in rows:
            name = QtWidgets.QLabel(f"{label}:")
            name.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
            field = QtWidgets.QLabel(str(value))
            field.setWordWrap(True)
            field.setStyleSheet("font-size: 12px; font-weight: 600;")
            field.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.vehicle_details_form.addRow(name, field)

        self.vehicle_details_empty.setVisible(not rows)

    def set_offline_mode(self, active: bool, message: str = "") -> None:
        """Show the explicit 'running in offline mode' banner."""
        if active:
            self.offline_banner.setText(
                message
                or "Offline mode — the server could not be reached at sign-in. "
                  "Decisions are queued locally."
            )
            self.offline_banner.show()
        else:
            self.offline_banner.clear()
            self.offline_banner.hide()

    def set_presence_hint(self, text: str) -> None:
        self.presence_hint_label.setText(f"Last state: {text}")

    def set_plate_text(self, plate: str) -> None:
        self.plate_input.setText(plate)

    def set_plate_detected(self, plate: str, confidence: float) -> None:
        """Prefill the plate field with AI result and show colour-coded confidence."""
        self.plate_input.setText(plate)
        pct = int(confidence * 100)
        if confidence >= 0.85:
            color = SUCCESS
        elif confidence >= 0.60:
            color = ORANGE
        else:
            color = DANGER
        self.ai_confidence_label.setText(f"AI: {pct}%")
        self.ai_confidence_label.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {color};")

    def clear_plate_detected(self) -> None:
        """Remove the AI confidence indicator (after a decision is submitted)."""
        self.ai_confidence_label.setText("")
        self.ai_confidence_label.setStyleSheet(f"font-size: 11px; color: {TEXT_MUTED};")

    def is_check_online(self) -> bool:
        return self.check_online_checkbox.isChecked()

    def enable_not_found_actions(self, enabled: bool) -> None:
        self.sync_recheck_button.setEnabled(enabled)
        self.add_temp_permit_button.setEnabled(enabled)

    # ==============================================================
    #  Staff attendance panel
    # ==============================================================

    @property
    def attendance_enabled(self) -> bool:
        return self._attendance_enabled

    def update_attendance_frame(self, image: QtGui.QImage) -> None:
        """Repaint the webcam preview with the positioning overlay on top."""
        if not self._attendance_enabled:
            return
        pixmap = QtGui.QPixmap.fromImage(image).scaled(
            self.attendance_camera_label.size(),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        self._draw_attendance_overlay(pixmap, image.width(), image.height())
        self.attendance_camera_label.setPixmap(pixmap)

    def set_face_detection(self, detection: DetectionFrame) -> None:
        """Take the latest boxes from the recognition pass.

        Stored rather than drawn: frames arrive ten times more often than
        detections, and the overlay has to ride along with them.
        """
        if not self._attendance_enabled:
            return
        self._detection = detection or EMPTY_DETECTION
        self._detection_at.restart()

    def _draw_attendance_overlay(
        self, pixmap: QtGui.QPixmap, frame_width: int, frame_height: int
    ) -> None:
        """Draw the guide frame and any face boxes onto the scaled pixmap.

        Drawing onto the already-scaled pixmap means one uniform scale factor
        and no letterbox offset — the offset that otherwise gets forgotten and
        puts every box a few pixels out.
        """
        if pixmap.isNull() or frame_width <= 0 or frame_height <= 0:
            return

        detection = self._detection
        if self._detection_at.elapsed() > DETECTION_TTL_MS:
            # Stale: the person left, or the pipeline stopped. Either way, do
            # not keep drawing a box around empty air.
            detection = EMPTY_DETECTION

        scale = preview_scale(
            frame_width, frame_height, pixmap.width(), pixmap.height()
        )
        painter = QtGui.QPainter(pixmap)
        try:
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            matched = detection.state == STATE_MATCHED
            colour = QtGui.QColor(_OVERLAY_MATCHED if matched else _OVERLAY_TRACKING)

            self._draw_guide(painter, frame_width, frame_height, scale, detection)
            for index, box in enumerate(detection.boxes):
                # Only the primary (largest) face gets the bright treatment;
                # bystanders in the background are drawn faintly so they do not
                # compete with the person actually being recognised.
                self._draw_face_box(
                    painter, box.scaled(scale), colour, primary=index == 0
                )
            self._draw_overlay_text(painter, pixmap, detection, colour)
        finally:
            painter.end()

    @staticmethod
    def _draw_guide(
        painter: QtGui.QPainter,
        frame_width: int,
        frame_height: int,
        scale: float,
        detection: DetectionFrame,
    ) -> None:
        """The dashed 'stand here' frame, faded once a face is in it."""
        guide = guide_rect(frame_width, frame_height).scaled(scale)
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 70 if detection.has_face else 130))
        pen.setWidth(BOX_LINE_WIDTH)
        pen.setStyle(QtCore.Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(
            guide.left, guide.top, guide.width, guide.height, 14, 14
        )

    @staticmethod
    def _draw_face_box(
        painter: QtGui.QPainter, box, colour: QtGui.QColor, primary: bool
    ) -> None:
        """Thin rectangle plus heavier corner brackets, as on the reference
        attendance station (live.html :: drawSingleFaceBox)."""
        if not primary:
            faded = QtGui.QColor(colour)
            faded.setAlpha(90)
            pen = QtGui.QPen(faded)
            pen.setWidth(BOX_LINE_WIDTH)
            painter.setPen(pen)
            painter.drawRect(box.left, box.top, box.width, box.height)
            return

        pen = QtGui.QPen(colour)
        pen.setWidth(BOX_LINE_WIDTH)
        painter.setPen(pen)
        painter.drawRect(box.left, box.top, box.width, box.height)

        # Corner brackets: they read as "aligned" far better than a plain
        # rectangle at a glance, which is the whole point of the overlay.
        pen.setWidth(CORNER_LINE_WIDTH)
        pen.setCapStyle(QtCore.Qt.FlatCap)
        painter.setPen(pen)
        length = min(CORNER_LENGTH, box.width // 2, box.height // 2)
        left, top, right, bottom = box.left, box.top, box.right, box.bottom
        for x, y, dx, dy in (
            (left, top, 1, 1),
            (right, top, -1, 1),
            (left, bottom, 1, -1),
            (right, bottom, -1, -1),
        ):
            painter.drawLine(x, y, x + dx * length, y)
            painter.drawLine(x, y, x, y + dy * length)

    @staticmethod
    def _draw_overlay_text(
        painter: QtGui.QPainter,
        pixmap: QtGui.QPixmap,
        detection: DetectionFrame,
        colour: QtGui.QColor,
    ) -> None:
        """The name, or the one positioning hint, on a legible plate.

        Text straight onto video is unreadable against a bright wall, so it gets
        a translucent background rather than an outline.
        """
        text = detection.label or detection.hint
        if not text:
            return
        font = painter.font()
        font.setPointSize(13)
        font.setBold(True)
        painter.setFont(font)

        metrics = QtGui.QFontMetrics(font)
        width = metrics.horizontalAdvance(text) + 24
        height = metrics.height() + 14
        left = (pixmap.width() - width) // 2
        top = pixmap.height() - height - 14

        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(2, 39, 47, 190))
        painter.drawRoundedRect(left, top, width, height, 6, 6)
        painter.setPen(QtGui.QPen(colour if detection.label else QtGui.QColor(WHITE)))
        painter.drawText(
            QtCore.QRect(left, top, width, height), QtCore.Qt.AlignCenter, text
        )

    def set_attendance_camera_status(self, connected: bool, message: str) -> None:
        if not self._attendance_enabled:
            return
        color = SUCCESS if connected else TEXT_MUTED
        self.attendance_camera_status_label.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )
        self.attendance_camera_status_label.setText(message)

    def apply_attendance_state(self, state: AttendancePanelState) -> None:
        """Show one recognition outcome, computed by ``attendance_display``.

        The wording and the level are decided there; this only paints them and
        arms the return to idle.
        """
        if not self._attendance_enabled:
            return
        background, foreground = _ATTENDANCE_LEVEL_COLORS.get(
            state.level, _ATTENDANCE_LEVEL_COLORS[LEVEL_IDLE]
        )
        self.attendance_state_label.setText(state.text)
        self.attendance_state_label.setStyleSheet(
            _attendance_state_style(background, foreground)
        )
        if state.transient:
            self._attendance_reset_timer.start(CONFIRMATION_HOLD_MS)
        else:
            self._attendance_reset_timer.stop()

    def clear_attendance_state(self) -> None:
        """Back to the idle 'Look at the camera' prompt."""
        if not self._attendance_enabled:
            return
        self._attendance_reset_timer.stop()
        state = attendance_idle()
        background, foreground = _ATTENDANCE_LEVEL_COLORS[LEVEL_IDLE]
        self.attendance_state_label.setText(state.text)
        self.attendance_state_label.setStyleSheet(
            _attendance_state_style(background, foreground)
        )

    def set_attendance_count(self, count: int) -> None:
        if not self._attendance_enabled:
            return
        self.attendance_count_label.setText(punch_count_text(count))

    def show_attendance_notice(self, text: str) -> None:
        """Raise the car-without-attendance reminder."""
        if not self._attendance_enabled or not text:
            return
        self.attendance_notice_banner.setText(text)
        self.attendance_notice_banner.show()

    def clear_attendance_notice(self) -> None:
        if not self._attendance_enabled:
            return
        self.attendance_notice_banner.clear()
        self.attendance_notice_banner.hide()

    # ==============================================================
    #  Barrier signal (visual only)
    # ==============================================================

    def flash_barrier_signal(self) -> None:
        """Light the indicator for a moment. Safe to call repeatedly."""
        self.barrier_signal_label.show()
        self._barrier_timer.start(BARRIER_FLASH_MS)

    def set_enrolment_status(self, text: str, level: str = ENROL_NEUTRAL) -> None:
        """Show how much of the roster is usable for recognition."""
        if not self._attendance_enabled:
            return
        background, foreground = _ENROLMENT_LEVEL_COLORS.get(
            level, _ENROLMENT_LEVEL_COLORS[ENROL_NEUTRAL]
        )
        self.enrolment_label.setText(text)
        self.enrolment_label.setStyleSheet(
            f"background-color: {background}; color: {foreground}; font-size: 12px;"
            " font-weight: 600; padding: 7px 10px; border-radius: 4px;"
        )
