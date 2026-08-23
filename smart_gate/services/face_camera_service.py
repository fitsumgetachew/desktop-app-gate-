"""Webcam worker for staff face attendance.

Built on the same QThread pattern as ``camera_service``: its own thread, its own
capture, reconnect backoff, and graceful degradation when the model or the
camera is unavailable — a station with no webcam logs a warning, reports a
status and keeps running the gate.

Two measured facts shape the loop, and both are load-bearing:

* ``face_locations`` costs 220 ms on a 640x480 frame but 67 ms on a half-scale
  copy, and still finds the face. So detection runs on the half-scale frame and
  the boxes are scaled back up before encoding, which then sees full-resolution
  pixels.
* The ALPR thread is already spending a 640x640 ONNX pass plus PaddleOCR at
  5 fps on this CPU. The face pipeline is throttled to ``FACE_MAX_FPS`` (~3) by
  timestamp, exactly as ``ALPR_MAX_FPS`` throttles that one; run both flat out
  and neither keeps up.

The worker owns its own SQLite connection (see ``worker_context``) so it can
write the punch itself; everything else it reports by signal. No UI here.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

import cv2
from PySide6 import QtCore, QtGui

from smart_gate.repositories.db import init_db
from smart_gate.repositories.device_repo import DeviceRepository
from smart_gate.repositories.punch_repo import PunchRepository
from smart_gate.repositories.staff_repo import StaffRepository
from smart_gate.services.attendance_service import AttendanceService
from smart_gate.services.face_overlay import (
    STATE_MATCHED,
    STATE_SEARCHING,
    STATE_TRACKING,
    DetectionFrame,
    build_detection,
)
from smart_gate.services.face_recognition_service import (
    MatchVoter,
)  # noqa: F401
from smart_gate.services.face_recognition_service import (
    FaceMatch,
    detect_and_encode,
    face_index,
)
from smart_gate.utils.config import AppConfig

logger = logging.getLogger(__name__)

# Reconnect backoff bounds (seconds), mirroring CameraWorker.
_RECONNECT_MIN_WAIT = 2.0
_RECONNECT_MAX_WAIT = 30.0

# Lower bound on the throttle interval, so a nonsensical FACE_MAX_FPS cannot
# turn the loop into a busy-wait. config.py clamps the value too.
_MIN_INTERVAL = 1.0 / 15.0


class FaceCameraWorker(QtCore.QThread):
    frame_ready = QtCore.Signal(QtGui.QImage)
    status_changed = QtCore.Signal(bool, str)
    face_recognised = QtCore.Signal(object)      # FaceMatch
    face_unrecognised = QtCore.Signal()          # a face, but nobody we know
    # The two outcomes of a recognition, carrying everything the panel needs so
    # it never has to query the database back on the UI thread.
    punch_recorded = QtCore.Signal(str, str, int)    # staff_uid, name, punch_time
    punch_suppressed = QtCore.Signal(str, str, int)  # staff_uid, name, earlier punch
    # Where the faces are, for the preview overlay. Emitted at the recognition
    # rate (~3 fps) while frames arrive at ~30; the view holds the last one and
    # expires it, rather than the worker pretending to detect on every frame.
    detection_changed = QtCore.Signal(object)        # DetectionFrame

    def __init__(self, config: AppConfig, db_path: Path) -> None:
        super().__init__()
        self._config = config
        self._db_path = db_path
        self._stop_flag = False
        self._last_pass_ts = 0.0
        self._conn: Optional[sqlite3.Connection] = None
        self._attendance: Optional[AttendanceService] = None
        self._last_frame = None
        # min_votes=1: commit on the first frame that clears the tolerance,
        # exactly as the department's running system does — every vote already
        # passed the distance gate, so waiting for a second one only added a
        # delay (and, on a borderline face, often an infinite one). The window
        # still does the useful half: a committed name survives ~5 passes
        # (~1.7 s) of misses, so it cannot strobe against "Not recognised".
        self._voter = MatchVoter(window=5, min_votes=1)

    def stop(self) -> None:
        self._stop_flag = True

    def get_last_frame(self):
        return self._last_frame

    @property
    def _interval(self) -> float:
        fps = getattr(self._config, "face_max_fps", 3.0) or 3.0
        return max(_MIN_INTERVAL, 1.0 / float(fps))

    # ------------------------------------------------------------------

    def run(self) -> None:
        self._stop_flag = False
        if not getattr(self._config, "face_attendance_enabled", True):
            logger.info("Face attendance disabled by configuration")
            self.status_changed.emit(False, "Attendance disabled")
            return

        try:
            self._open_resources()
        except Exception:
            logger.warning("Face attendance could not open its database", exc_info=True)
            self.status_changed.emit(False, "Attendance unavailable")
            return

        try:
            self._capture_loop()
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _open_resources(self) -> None:
        """Thread-local connection + repositories, per the worker_context rule."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        init_db(self._conn)

        device = DeviceRepository(self._conn).get_device()
        self._attendance = AttendanceService(
            PunchRepository(self._conn),
            device_id=device.device_id if device else "",
            gate_id=(device.gate_id if device else "") or self._config.gate_id,
            lane_id=(device.lane_id if device else "") or self._config.lane_id,
        )
        # If the sync thread has not primed the index yet, do it here so a gate
        # that starts offline still recognises last night's roster.
        if len(face_index) == 0:
            face_index.load_from_repo(StaffRepository(self._conn))

    def _capture_loop(self) -> None:
        reconnect_wait = _RECONNECT_MIN_WAIT
        while not self._stop_flag:
            cap = self._open_capture()
            if cap is None:
                self.status_changed.emit(
                    False, f"Attendance camera unavailable — retrying in {reconnect_wait:.0f}s"
                )
                time.sleep(reconnect_wait)
                reconnect_wait = min(reconnect_wait * 2, _RECONNECT_MAX_WAIT)
                continue
            reconnect_wait = _RECONNECT_MIN_WAIT
            self._warmup()
            self.status_changed.emit(True, "Attendance camera connected")

            while not self._stop_flag:
                ok, frame = cap.read()
                if not ok:
                    self.status_changed.emit(False, "Attendance camera disconnected")
                    break
                self._last_frame = frame

                now = time.monotonic()
                if (now - self._last_pass_ts) >= self._interval:
                    self._last_pass_ts = now
                    self._process_frame(frame)

                image = self._to_qimage(frame)
                if image is not None:
                    self.frame_ready.emit(image)
                time.sleep(0.03)   # USB capture returns instantly; pace the loop

            cap.release()
            time.sleep(1)

    def _warmup(self) -> None:
        """Pay dlib's model-load cost before the first real frame.

        Measured on this machine: the first detection takes ~836 ms while the
        HOG model loads, then settles to ~30 ms idle and ~60 ms with a face. Left
        unwarmed that spike lands on the first person to walk up, and on a
        thread that is also feeding the preview. Same reason CameraWorker warms
        the ALPR pipeline up before its frame loop.
        """
        try:
            import numpy as np  # noqa: PLC0415

            detect_and_encode(np.zeros((120, 160, 3), dtype=np.uint8))
            logger.info("Face pipeline warmed up")
        except Exception:
            # A warmup that fails is not a reason to skip attendance entirely;
            # the real frames will simply pay the cost instead.
            logger.debug("Face warmup failed", exc_info=True)

    def _process_frame(self, frame) -> None:
        """One throttled recognition pass. Never raises into the capture loop."""
        height, width = (frame.shape[0], frame.shape[1]) if frame is not None else (0, 0)
        try:
            faces = detect_and_encode(frame)
        except Exception:
            logger.debug("Face pipeline error on a frame", exc_info=True)
            return

        if not faces:
            # Emitted every pass, not only on change: this is what clears a box
            # left behind by someone who has walked away.
            self.detection_changed.emit(
                build_detection((), width, height, STATE_SEARCHING)
            )
            return

        boxes = [face.box for face in faces]
        tolerance = getattr(self._config, "face_tolerance", 0.50)
        min_confidence = getattr(self._config, "face_min_confidence", 45.0)
        match: Optional[FaceMatch] = None
        for face in faces:
            if face.encoding is None:
                continue          # too small to embed — drawn, but not matched
            candidate = face_index.identify(face.encoding, tolerance, min_confidence)
            # Several people can share the frame; the closest one wins.
            if candidate is not None and (
                match is None or candidate.distance < match.distance
            ):
                match = candidate

        # A face sitting near the tolerance flips side to side between frames;
        # voting over a short window is what stops a name strobing against
        # "Not recognised". Every vote still cleared the threshold on its own.
        match = self._voter.vote(match)

        self.detection_changed.emit(
            build_detection(
                boxes,
                width,
                height,
                STATE_MATCHED if match else STATE_TRACKING,
                match.full_name if match else None,
            )
        )

        if match is None:
            self.face_unrecognised.emit()
            return

        self.face_recognised.emit(match)
        if self._attendance is None:
            return
        try:
            outcome = self._attendance.record_punch(match)
        except Exception:
            logger.warning("Could not record attendance punch", exc_info=True)
            return
        if outcome.recorded and outcome.punch is not None:
            self.punch_recorded.emit(
                outcome.punch.staff_uid, match.full_name, outcome.punch.punch_time
            )
        elif outcome.suppressed and outcome.suppressed_since is not None:
            # Not a failure — they already punched. The panel says so gently.
            self.punch_suppressed.emit(
                match.staff_uid, match.full_name, outcome.suppressed_since
            )

    def _open_capture(self):
        index = getattr(self._config, "face_camera_index", 0)
        try:
            cap = cv2.VideoCapture(index)
        except Exception:
            logger.warning("Attendance camera %s failed to open", index, exc_info=True)
            return None
        if not cap.isOpened():
            cap.release()
            logger.warning("Attendance camera %s is not available", index)
            return None
        return cap

    @staticmethod
    def _to_qimage(frame) -> Optional[QtGui.QImage]:
        if frame is None:
            return None
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channel = rgb.shape
        return QtGui.QImage(
            rgb.data, width, height, channel * width, QtGui.QImage.Format_RGB888
        ).copy()


class FaceCameraService(QtCore.QObject):
    """Thin owner of the worker thread, matching ``CameraService``'s shape."""

    frame_ready = QtCore.Signal(QtGui.QImage)
    status_changed = QtCore.Signal(bool, str)
    face_recognised = QtCore.Signal(object)
    face_unrecognised = QtCore.Signal()
    punch_recorded = QtCore.Signal(str, str, int)
    punch_suppressed = QtCore.Signal(str, str, int)
    detection_changed = QtCore.Signal(object)

    def __init__(self, config: AppConfig, db_path: Path) -> None:
        super().__init__()
        self.worker = FaceCameraWorker(config, db_path)
        self.worker.frame_ready.connect(self.frame_ready.emit)
        self.worker.status_changed.connect(self.status_changed.emit)
        self.worker.face_recognised.connect(self.face_recognised.emit)
        self.worker.face_unrecognised.connect(self.face_unrecognised.emit)
        self.worker.punch_recorded.connect(self.punch_recorded.emit)
        self.worker.punch_suppressed.connect(self.punch_suppressed.emit)
        self.worker.detection_changed.connect(self.detection_changed.emit)

    def start(self) -> None:
        if not self.worker.isRunning():
            self.worker.start()

    def stop(self) -> None:
        self.worker.stop()
        self.worker.wait(3000)
