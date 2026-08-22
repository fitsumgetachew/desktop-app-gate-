"""SIT branding theme – colours, fonts, and QSS stylesheet.

This module is the **single source of truth** for all visual tokens used
across the Smart Gate Desktop UI.  Import ``SIT_STYLESHEET`` and apply it
once on the QApplication (or per-widget) to get consistent styling.
"""

from __future__ import annotations

from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _asset_url(name: str) -> str:
    """Absolute asset path in the forward-slash form Qt stylesheets expect.

    Qt's stylesheet parser wants ``/`` even on Windows, so ``as_posix`` rather
    than ``str``.
    """
    return _ASSETS_DIR.joinpath(name).as_posix()


# Dropdown and spin-box arrows. Drawn from SVG rather than with the CSS
# border-triangle trick, which Qt renders as a filled rectangle — two dark
# blocks on every spin box. A missing file degrades to no arrow, not a block.
CHEVRON_DOWN = _asset_url("chevron_down.svg")
CHEVRON_UP = _asset_url("chevron_up.svg")

# ---------------------------------------------------------------------------
# SIT colour palette
# ---------------------------------------------------------------------------
DAINTREE = "#02272F"          # primary dark
ORANGE = "#FE5823"            # primary accent
ORANGE_ALT = "#F15A24"        # style-guide variant
HALF_BAKED = "#8AC4C7"        # secondary
YELLOW = "#EEBE41"            # accent / warning
LIGHT_BLUE = "#CFDFDC"        # backgrounds
LIGHT_YELLOW = "#EAE4D2"      # alternate bg

# Derived / helper colours
WHITE = "#FFFFFF"
OFF_WHITE = "#F5F5F3"
BORDER = "#C4D4D1"
BORDER_FOCUS = ORANGE
TEXT_PRIMARY = DAINTREE
TEXT_SECONDARY = "#4A6A70"
TEXT_MUTED = "#6B8E93"
DANGER = "#D9534F"
SUCCESS = "#3DAA6D"

# ---------------------------------------------------------------------------
# Traffic-light decision states
# ---------------------------------------------------------------------------
# The three states the camera view can be driven into when the ALPR commits a
# plate. Each has a saturated border/banner colour (read at a glance from
# several metres away, in daylight) and a soft tint for detail panels.
# Derived from the palette above so the gate view still reads as SIT:
# green from SUCCESS, red from DANGER, orange from the SIT accent ORANGE.

STATE_GREEN = "#2E8B57"        # deeper SUCCESS — passes contrast on white text
STATE_GREEN_SOFT = "#E4F3EA"
STATE_RED = "#C62828"          # deeper DANGER — unmistakably an alarm
STATE_RED_SOFT = "#FBE6E5"
STATE_ORANGE = ORANGE          # SIT primary accent
STATE_ORANGE_SOFT = "#FFEDE5"

# Thickness of the border drawn around the live video feed per state.
STATE_BORDER_WIDTH = "6px"

# ---------------------------------------------------------------------------
# Font stack
# ---------------------------------------------------------------------------
FONT_FAMILY = "'Poppins', 'Segoe UI', 'Helvetica Neue', 'Ubuntu', sans-serif"
FONT_SIZE_BASE = "13px"
FONT_SIZE_SMALL = "11px"
FONT_SIZE_LARGE = "15px"
FONT_SIZE_HEADER = "16px"

# ---------------------------------------------------------------------------
# Shared geometry tokens
# ---------------------------------------------------------------------------
RADIUS = "6px"
RADIUS_SMALL = "4px"
RADIUS_LARGE = "10px"

# ---------------------------------------------------------------------------
# QSS Stylesheet
# ---------------------------------------------------------------------------

SIT_STYLESHEET = f"""
/* ── Global ────────────────────────────────────────────────────── */
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BASE};
    color: {TEXT_PRIMARY};
}}

QMainWindow {{
    background-color: {OFF_WHITE};
}}

/* ── Top header bar ───────────────────────────────────────────── */
QFrame#HeaderBar {{
    background-color: {DAINTREE};
    min-height: 52px;
    max-height: 52px;
    padding: 0 16px;
}}
QFrame#HeaderBar QLabel {{
    color: {WHITE};
    font-size: {FONT_SIZE_BASE};
}}
QFrame#HeaderBar QLabel#HeaderTitle {{
    font-size: {FONT_SIZE_HEADER};
    font-weight: 600;
    color: {WHITE};
    padding-left: 8px;
}}
QFrame#HeaderBar QLabel#HeaderBadgeOnline {{
    background-color: {SUCCESS};
    color: {WHITE};
    border-radius: 3px;
    padding: 2px 8px;
    font-size: {FONT_SIZE_SMALL};
    font-weight: 600;
}}
QFrame#HeaderBar QLabel#HeaderBadgeOffline {{
    background-color: {DANGER};
    color: {WHITE};
    border-radius: 3px;
    padding: 2px 8px;
    font-size: {FONT_SIZE_SMALL};
    font-weight: 600;
}}
QFrame#HeaderBar QPushButton {{
    background-color: transparent;
    color: {WHITE};
    border: 1px solid rgba(255,255,255,0.25);
    border-radius: {RADIUS_SMALL};
    padding: 5px 14px;
    font-size: {FONT_SIZE_SMALL};
    font-weight: 500;
}}
QFrame#HeaderBar QPushButton:hover {{
    background-color: rgba(255,255,255,0.12);
    border-color: rgba(255,255,255,0.5);
}}
QFrame#HeaderBar QPushButton:pressed {{
    background-color: rgba(255,255,255,0.20);
}}
QFrame#HeaderBar QPushButton#HeaderSyncBtn {{
    background-color: {ORANGE};
    border: none;
    color: {WHITE};
    font-weight: 600;
}}
QFrame#HeaderBar QPushButton#HeaderSyncBtn:hover {{
    background-color: {ORANGE_ALT};
}}

/* ── Sync strip ───────────────────────────────────────────────── */
QFrame#SyncStrip {{
    background-color: {LIGHT_BLUE};
    min-height: 30px;
    max-height: 30px;
    padding: 0 16px;
}}
QFrame#SyncStrip QLabel {{
    color: {TEXT_SECONDARY};
    font-size: {FONT_SIZE_SMALL};
}}

/* ── Panels ───────────────────────────────────────────────────── */
QFrame#LeftPanel, QFrame#RightPanel {{
    background-color: {WHITE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LARGE};
}}

/* ── Camera area ──────────────────────────────────────────────── */
QLabel#CameraPreview {{
    background-color: #1A1A1A;
    color: #888;
    border-radius: {RADIUS};
    font-size: {FONT_SIZE_LARGE};
}}

QLabel#CameraStatus {{
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_SMALL};
    padding: 2px 0;
}}

/* ── Inputs ───────────────────────────────────────────────────── */
QLineEdit {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 7px 10px;
    background-color: {WHITE};
    font-size: {FONT_SIZE_BASE};
    color: {TEXT_PRIMARY};
    selection-background-color: {HALF_BAKED};
}}
QLineEdit:focus {{
    border-color: {ORANGE};
    outline: none;
}}
QLineEdit:disabled {{
    background-color: {LIGHT_BLUE};
    color: {TEXT_MUTED};
}}

/* QSpinBox does NOT match QDoubleSpinBox — they are siblings, not parent and
   child — so both are named. And a spin box with a styled border loses the
   native rendering of its arrows: unstyled sub-controls then fall back to a
   dark default that reads as two black blocks. */
QSpinBox, QDoubleSpinBox {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 6px 10px;
    background-color: {WHITE};
    font-size: {FONT_SIZE_BASE};
    color: {TEXT_PRIMARY};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ORANGE};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background-color: transparent;
    border: none;
    width: 18px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({CHEVRON_UP});
    width: 10px;
    height: 6px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({CHEVRON_DOWN});
    width: 10px;
    height: 6px;
}}

QComboBox {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 6px 10px;
    background-color: {WHITE};
    font-size: {FONT_SIZE_BASE};
    color: {TEXT_PRIMARY};
}}
QComboBox:focus {{
    border-color: {ORANGE};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
/* Without this the arrow is the unstyled default and the field does not read
   as a dropdown at all. */
QComboBox::down-arrow {{
    image: url({CHEVRON_DOWN});
    width: 10px;
    height: 6px;
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {WHITE};
    border: 1px solid {BORDER};
    selection-background-color: {LIGHT_BLUE};
    selection-color: {TEXT_PRIMARY};
}}

QCheckBox {{
    spacing: 6px;
    font-size: {FONT_SIZE_BASE};
    color: {TEXT_PRIMARY};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {WHITE};
}}
QCheckBox::indicator:checked {{
    background-color: {ORANGE};
    border-color: {ORANGE};
}}

/* Small square actions (the camera list's remove button). Unstyled these
   render as solid dark blocks against the light card. */
QToolButton {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SMALL};
    background-color: {WHITE};
    color: {TEXT_SECONDARY};
    font-size: {FONT_SIZE_BASE};
    font-weight: 700;
}}
QToolButton:hover {{
    background-color: {STATE_RED_SOFT};
    border-color: {DANGER};
    color: {DANGER};
}}

/* ── Buttons ──────────────────────────────────────────────────── */
QPushButton {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    padding: 8px 18px;
    font-size: {FONT_SIZE_BASE};
    font-weight: 500;
    background-color: {WHITE};
    color: {TEXT_PRIMARY};
}}
QPushButton:hover {{
    background-color: {LIGHT_BLUE};
    border-color: {HALF_BAKED};
}}
QPushButton:pressed {{
    background-color: {HALF_BAKED};
}}
QPushButton:disabled {{
    background-color: {LIGHT_BLUE};
    color: {TEXT_MUTED};
    border-color: {BORDER};
}}

/* Primary orange button */
QPushButton#PrimaryBtn, QPushButton#AllowBtn {{
    background-color: {ORANGE};
    color: {WHITE};
    border: none;
    font-weight: 600;
}}
QPushButton#PrimaryBtn:hover, QPushButton#AllowBtn:hover {{
    background-color: {ORANGE_ALT};
}}
QPushButton#PrimaryBtn:pressed, QPushButton#AllowBtn:pressed {{
    background-color: #D94D1F;
}}

/* Allow button - green tint */
QPushButton#AllowBtn {{
    background-color: {SUCCESS};
}}
QPushButton#AllowBtn:hover {{
    background-color: #34955F;
}}

/* Deny button */
QPushButton#DenyBtn {{
    background-color: {DANGER};
    color: {WHITE};
    border: none;
    font-weight: 600;
}}
QPushButton#DenyBtn:hover {{
    background-color: #C9453F;
}}

/* Capture button */
QPushButton#CaptureBtn {{
    background-color: {HALF_BAKED};
    color: {DAINTREE};
    border: none;
    font-weight: 600;
}}
QPushButton#CaptureBtn:hover {{
    background-color: #7AB5B8;
}}

/* Secondary accent */
QPushButton#SecondaryBtn {{
    background-color: {LIGHT_BLUE};
    color: {DAINTREE};
    border: 1px solid {HALF_BAKED};
}}
QPushButton#SecondaryBtn:hover {{
    background-color: {HALF_BAKED};
    color: {WHITE};
}}

/* ── Table ────────────────────────────────────────────────────── */
QTableWidget {{
    background-color: {WHITE};
    gridline-color: {BORDER};
    border: none;
    font-size: {FONT_SIZE_BASE};
    selection-background-color: {LIGHT_BLUE};
    selection-color: {TEXT_PRIMARY};
}}
QTableWidget::item {{
    padding: 6px 8px;
}}
QHeaderView::section {{
    background-color: {DAINTREE};
    color: {WHITE};
    font-weight: 600;
    font-size: {FONT_SIZE_SMALL};
    padding: 8px 8px;
    border: none;
    border-right: 1px solid rgba(255,255,255,0.15);
}}
QTableWidget::item:alternate {{
    background-color: {OFF_WHITE};
}}

/* ── Scrollbar ────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {OFF_WHITE};
    width: 10px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {HALF_BAKED};
    min-height: 30px;
    border-radius: 5px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: {OFF_WHITE};
    height: 10px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal {{
    background: {HALF_BAKED};
    min-width: 30px;
    border-radius: 5px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* ── Splitter ─────────────────────────────────────────────────── */
QSplitter::handle {{
    background-color: {BORDER};
    width: 3px;
}}
QSplitter::handle:hover {{
    background-color: {HALF_BAKED};
}}

/* ── Group box (settings) ─────────────────────────────────────── */
QGroupBox {{
    font-weight: 600;
    font-size: {FONT_SIZE_BASE};
    color: {DAINTREE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS};
    margin-top: 16px;
    padding: 20px 12px 12px 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 10px;
    background-color: {WHITE};
    border-radius: {RADIUS_SMALL};
}}

/* ── Tooltip ──────────────────────────────────────────────────── */
QToolTip {{
    background-color: {DAINTREE};
    color: {WHITE};
    border: none;
    padding: 5px 8px;
    border-radius: {RADIUS_SMALL};
    font-size: {FONT_SIZE_SMALL};
}}

/* ── Login page ───────────────────────────────────────────────── */
QFrame#LoginCard {{
    background-color: {WHITE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LARGE};
    padding: 32px;
}}
QFrame#LoginCard QLabel#LoginTitle {{
    font-size: 22px;
    font-weight: 700;
    color: {DAINTREE};
}}
QFrame#LoginCard QLabel#LoginSubtitle {{
    font-size: {FONT_SIZE_BASE};
    color: {TEXT_MUTED};
}}
QFrame#LoginCard QLineEdit {{
    padding: 10px 12px;
    font-size: {FONT_SIZE_BASE};
}}

/* ── Settings page ────────────────────────────────────────────── */
QFrame#SettingsCard {{
    background-color: {WHITE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LARGE};
    padding: 24px;
}}

/* Ensure primary/action buttons always render with correct colours
   regardless of platform style or parent background. */
QPushButton#PrimaryBtn {{
    background-color: {ORANGE};
    color: {WHITE};
    border: none;
    font-weight: 600;
}}
QPushButton#PrimaryBtn:hover {{
    background-color: {ORANGE_ALT};
    color: {WHITE};
}}
QPushButton#PrimaryBtn:pressed {{
    background-color: #D94D1F;
    color: {WHITE};
}}
QPushButton#PrimaryBtn:disabled {{
    background-color: #C0B0A8;
    color: #E0D8D4;
}}

QPushButton#LoginBtn {{
    background-color: {ORANGE};
    color: {WHITE};
    border: none;
    font-weight: 600;
    font-size: {FONT_SIZE_LARGE};
    padding: 10px 0;
    border-radius: {RADIUS};
}}
QPushButton#LoginBtn:hover {{
    background-color: {ORANGE_ALT};
    color: {WHITE};
}}
QPushButton#LoginBtn:pressed {{
    background-color: #D94D1F;
    color: {WHITE};
}}
"""


def get_logo_path(variant: str = "light") -> str | None:
    """Return the path to the SIT logo if it exists on disk.

    Parameters
    ----------
    variant : str
        ``"light"`` or ``"dark"``.  Falls back to the other variant or
        ``None`` when neither exists.
    """
    import os
    from pathlib import Path

    # Look next to this file: smart_gate/ui/ -> smart_gate/assets/
    assets_dir = Path(__file__).resolve().parent.parent / "assets"

    for ext in ("png", "svg"):
        candidate = assets_dir / f"logo_{variant}.{ext}"
        if candidate.exists():
            return str(candidate)

    # Fallback to opposite variant
    other = "dark" if variant == "light" else "light"
    for ext in ("png", "svg"):
        candidate = assets_dir / f"logo_{other}.{ext}"
        if candidate.exists():
            return str(candidate)

    return None
