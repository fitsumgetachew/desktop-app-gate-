"""The plate read zone: a software zoom for a camera that sees too much.

A gate camera often has to be mounted where it can — watching a whole yard
while the cars pass through one corner of the frame. The ALPR letterboxes its
input down to 640x640, so a plate that is small in the frame arrives at the
detector a dozen pixels wide and simply does not exist as far as OCR is
concerned.

Cropping the frame to the region the cars actually pass through — BEFORE the
letterbox — hands the detector the same plate at several times the pixel
density. No new camera, no optical zoom: the pixels were always there, the
pipeline was throwing them away.

The zone is configured as four fractions of the frame, ``x,y,w,h`` — e.g.
``0.5,0.3,0.5,0.5`` is the right half, starting 30% down. Fractions, not
pixels, so the same setting survives a stream resolution change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# A zone smaller than this fraction of the frame in either direction is almost
# certainly a typo, and would crop to a sliver the detector cannot use.
MIN_FRACTION = 0.05


@dataclass(frozen=True)
class ReadZone:
    x: float
    y: float
    w: float
    h: float

    def pixel_rect(self, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """``(x1, y1, x2, y2)`` in pixels for a frame of the given size."""
        x1 = int(round(self.x * frame_w))
        y1 = int(round(self.y * frame_h))
        x2 = int(round((self.x + self.w) * frame_w))
        y2 = int(round((self.y + self.h) * frame_h))
        # Clamp defensively: rounding at the edges must never produce an
        # empty or out-of-bounds crop.
        x1 = max(0, min(x1, frame_w - 1))
        y1 = max(0, min(y1, frame_h - 1))
        x2 = max(x1 + 1, min(x2, frame_w))
        y2 = max(y1 + 1, min(y2, frame_h))
        return x1, y1, x2, y2

    def apply(self, frame):
        """The cropped view of ``frame`` (a numpy array view, no copy)."""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.pixel_rect(w, h)
        return frame[y1:y2, x1:x2]


def parse_read_zone(raw: Optional[str]) -> Optional[ReadZone]:
    """``"x,y,w,h"`` fractions → a validated zone, or ``None`` for full frame.

    Empty and invalid both mean "read the whole frame": a malformed setting
    must degrade to today's behaviour, never to a zone nobody chose.
    """
    text = (raw or "").strip()
    if not text:
        return None
    parts = text.split(",")
    if len(parts) != 4:
        logger.warning("ALPR_ROI %r is not x,y,w,h — reading the full frame", raw)
        return None
    try:
        x, y, w, h = (float(p.strip()) for p in parts)
    except ValueError:
        logger.warning("ALPR_ROI %r is not numeric — reading the full frame", raw)
        return None
    if not (0.0 <= x < 1.0 and 0.0 <= y < 1.0):
        logger.warning("ALPR_ROI %r: origin outside the frame — reading the full frame", raw)
        return None
    if w < MIN_FRACTION or h < MIN_FRACTION:
        logger.warning("ALPR_ROI %r: zone too small — reading the full frame", raw)
        return None
    if x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
        logger.warning("ALPR_ROI %r: zone leaves the frame — reading the full frame", raw)
        return None
    return ReadZone(x=x, y=y, w=min(w, 1.0 - x), h=min(h, 1.0 - y))
