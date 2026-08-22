"""Finding the cameras actually attached to this machine.

Opening a camera is slow (a few hundred ms each, sometimes seconds for one that
is powered but not streaming) and OpenCV logs noisily at every missing index, so
this never runs on the UI thread — ``DiscoveryWorker`` exists for that.

The probing itself is injectable so the scan logic can be tested without any
hardware at all.
"""

from __future__ import annotations

import glob
import logging
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from PySide6 import QtCore

logger = logging.getLogger(__name__)

# How far to look when the platform gives us no better hint. Beyond this is
# almost always empty, and each miss costs real time.
MAX_PROBE_INDEX = 8


@dataclass(frozen=True)
class DiscoveredCamera:
    index: int
    width: int = 0
    height: int = 0

    @property
    def label(self) -> str:
        if self.width and self.height:
            return f"USB device {self.index} ({self.width}x{self.height})"
        return f"USB device {self.index}"


def candidate_indices(max_index: int = MAX_PROBE_INDEX) -> List[int]:
    """Indices worth probing.

    On Linux ``/dev/video*`` says which device nodes exist, which avoids paying
    the open-timeout for indices that were never going to work. Some of those
    nodes are metadata rather than capture devices, so they still get probed —
    this only narrows the search, it does not decide the answer.
    """
    nodes = sorted(glob.glob("/dev/video*"))
    if nodes:
        found = []
        for node in nodes:
            match = re.search(r"(\d+)$", node)
            if match:
                index = int(match.group(1))
                if index <= max_index and index not in found:
                    found.append(index)
        if found:
            return found
    return list(range(max_index + 1))


def probe_index(index: int) -> Optional[DiscoveredCamera]:
    """Open one index and see whether it yields a frame.

    A device that opens but never delivers a frame is not a camera we can use —
    on Linux that is usually a metadata node sitting next to the real one.
    """
    try:
        import cv2  # noqa: PLC0415 — heavy, and only needed for a scan
    except Exception:
        logger.warning("OpenCV unavailable — cannot scan for cameras", exc_info=True)
        return None

    cap = None
    try:
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        height, width = frame.shape[0], frame.shape[1]
        return DiscoveredCamera(index=index, width=int(width), height=int(height))
    except Exception:
        logger.debug("Probe of camera index %s failed", index, exc_info=True)
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def discover_cameras(
    probe: Callable[[int], Optional[DiscoveredCamera]] = probe_index,
    indices: Optional[Sequence[int]] = None,
) -> List[DiscoveredCamera]:
    """Every USB camera that actually produced a frame, in index order."""
    to_probe = list(indices) if indices is not None else candidate_indices()
    found = [camera for camera in (probe(i) for i in to_probe) if camera is not None]
    logger.info("Camera scan: probed %d indices, found %d", len(to_probe), len(found))
    return found


class DiscoveryWorker(QtCore.QThread):
    """Runs :func:`discover_cameras` off the UI thread.

    A scan can take several seconds with nothing attached; doing it inline would
    freeze the settings window and look like a crash.
    """

    finished_scan = QtCore.Signal(object)      # List[DiscoveredCamera]
    failed = QtCore.Signal(str)

    def run(self) -> None:
        try:
            self.finished_scan.emit(discover_cameras())
        except Exception as exc:
            logger.warning("Camera scan failed", exc_info=True)
            self.failed.emit(type(exc).__name__)
