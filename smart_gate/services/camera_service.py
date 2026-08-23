from __future__ import annotations

import logging
import os
import time
from typing import Optional

import cv2
from PySide6 import QtCore, QtGui

from smart_gate.utils.paths import get_detector_model_path
from smart_gate.utils.roi import ReadZone

logger = logging.getLogger(__name__)


# The detector + OCR cost far more than a frame interval, so running them on
# every frame starves the preview. 5 analyses/second is ample for a vehicle
# rolling up to a barrier, and the preview keeps streaming at full rate.
ALPR_MAX_FPS = 5.0
ALPR_MIN_INTERVAL = 1.0 / ALPR_MAX_FPS

# FFmpeg options for RTSP capture (Hikvision & co.): interleaved TCP instead
# of UDP — UDP drops packets on long cable runs / busy switches and produces
# smeared, half-decoded frames that poison the ALPR pipeline.
_RTSP_FFMPEG_OPTIONS = "rtsp_transport;tcp"

# Fail fast on a powered-off/unreachable camera instead of OpenCV's 30 s
# default. Enforced by OpenCV's interrupt callback, which works regardless of
# the FFmpeg build's RTSP option names.
_RTSP_OPEN_TIMEOUT_MS = 6000
_RTSP_READ_TIMEOUT_MS = 6000

# Reconnect backoff bounds (seconds) when a stream drops or fails to open.
_RECONNECT_MIN_WAIT = 1.0
_RECONNECT_MAX_WAIT = 15.0


class CameraWorker(QtCore.QThread):
    frame_ready = QtCore.Signal(QtGui.QImage)
    status_changed = QtCore.Signal(bool, str)
    # plate, confidence, raw_text, ocr_confidence, crop (numpy array as object)
    plate_detected = QtCore.Signal(str, float, str, float, object)

    def __init__(
        self,
        mode: str,
        index: int,
        rtsp_url: str,
        read_zone: Optional[ReadZone] = None,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.index = index
        self.rtsp_url = rtsp_url
        # The plate read zone: the AI sees only this crop, at full pixel
        # density — a software zoom for a camera that watches a whole yard.
        # The preview still shows the entire frame, with the zone outlined so
        # the operator can see (and aim) what is actually being read.
        self.read_zone = read_zone
        self._stop_flag = False
        self._last_frame = None
        self._recognizer = None
        self._last_alpr_ts = 0.0

    def stop(self) -> None:
        self._stop_flag = True

    def reset_recognizer(self) -> None:
        """Clear the ALPR frame buffer between vehicles."""
        if self._recognizer is not None:
            self._recognizer.reset()

    def get_last_frame(self):
        return self._last_frame

    def run(self) -> None:
        self._stop_flag = False  # reset on each start so re-login works
        self._recognizer = self._create_recognizer()
        reconnect_wait = _RECONNECT_MIN_WAIT

        while not self._stop_flag:
            cap = self._open_capture()
            if not cap:
                self.status_changed.emit(
                    False, f"Camera open failed — retrying in {reconnect_wait:.0f}s"
                )
                time.sleep(reconnect_wait)
                reconnect_wait = min(reconnect_wait * 2, _RECONNECT_MAX_WAIT)
                continue
            reconnect_wait = _RECONNECT_MIN_WAIT

            # Warm up models once after camera opens, before the frame loop
            if self._recognizer is not None:
                try:
                    self._recognizer.warmup()
                    logger.info("ALPR recognizer warmed up")
                except Exception:
                    logger.warning("ALPR warmup failed — running without plate recognition", exc_info=True)
                    self._recognizer = None

            self.status_changed.emit(True, "Camera connected")
            while not self._stop_flag:
                ret, frame = cap.read()
                if not ret:
                    self.status_changed.emit(False, "Camera disconnected")
                    break
                self._last_frame = frame

                # Run the ALPR pipeline at ALPR_MAX_FPS, skipping frames by
                # timestamp; the preview below still gets every frame.
                now = time.monotonic()
                if (
                    self._recognizer is not None
                    and (now - self._last_alpr_ts) >= ALPR_MIN_INTERVAL
                ):
                    self._last_alpr_ts = now
                    try:
                        alpr_view = (
                            self.read_zone.apply(frame)
                            if self.read_zone is not None
                            else frame
                        )
                        result = self._recognizer.process_frame(alpr_view)
                        if result is not None:
                            self.plate_detected.emit(
                                result.plate_number,
                                result.confidence,
                                result.raw_text,
                                result.ocr_confidence,
                                result.crop,
                            )
                    except Exception:
                        logger.debug("ALPR process_frame error", exc_info=True)

                image = self._to_qimage(self._preview_frame(frame))
                if image is not None:
                    self.frame_ready.emit(image)
                # USB capture returns instantly, so pace the loop manually.
                # RTSP reads block until the next frame arrives — sleeping on
                # top of that lets frames queue in FFmpeg's buffer and the
                # ALPR ends up analyzing seconds-old video.
                if not self._is_rtsp():
                    time.sleep(0.03)
            cap.release()
            time.sleep(1)

    @staticmethod
    def _create_recognizer():
        """Try to load the ALPR pipeline. Returns None if deps are missing or model not found."""
        try:
            from smart_gate.services.alpr_pipeline import PlateRecognizer, PlateRecognizerConfig
            model_path = get_detector_model_path()
            config = PlateRecognizerConfig(detector_path=str(model_path))
            return PlateRecognizer(config)
        except Exception:
            logger.warning("ALPR pipeline not available — camera running without plate recognition", exc_info=True)
            return None

    def _is_rtsp(self) -> bool:
        return self.mode.upper() == "RTSP"

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        if self._is_rtsp():
            if not self.rtsp_url:
                return None
            # Must be set before VideoCapture is created — OpenCV reads it once
            # when the FFmpeg backend opens the stream.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _RTSP_FFMPEG_OPTIONS
            cap = cv2.VideoCapture(
                self.rtsp_url,
                cv2.CAP_FFMPEG,
                [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, _RTSP_OPEN_TIMEOUT_MS,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, _RTSP_READ_TIMEOUT_MS,
                ],
            )
            # Keep at most one frame buffered so reads always return the most
            # recent frame (low latency matters more than smoothness here).
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _preview_frame(self, frame):
        """The frame the guard sees: full view, read zone outlined.

        Drawn on a copy — ``_last_frame`` is what evidence captures use, and an
        orange rectangle must never end up burned into an evidence photo.
        """
        if self.read_zone is None or frame is None:
            return frame
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.read_zone.pixel_rect(w, h)
        shown = frame.copy()
        cv2.rectangle(shown, (x1, y1), (x2, y2), (0, 122, 255), 2)
        cv2.putText(
            shown, "PLATE READ ZONE", (x1 + 6, max(y1 - 8, 16)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 122, 255), 2,
        )
        return shown

    @staticmethod
    def _to_qimage(frame) -> Optional[QtGui.QImage]:
        if frame is None:
            return None
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channel = rgb.shape
        bytes_per_line = channel * width
        return QtGui.QImage(rgb.data, width, height, bytes_per_line, QtGui.QImage.Format_RGB888).copy()


class CameraService(QtCore.QObject):
    frame_ready = QtCore.Signal(QtGui.QImage)
    status_changed = QtCore.Signal(bool, str)
    # plate, confidence, raw_text, ocr_confidence, crop (numpy array as object)
    plate_detected = QtCore.Signal(str, float, str, float, object)

    def __init__(
        self,
        mode: str,
        index: int,
        rtsp_url: str,
        read_zone: Optional[ReadZone] = None,
    ) -> None:
        super().__init__()
        self.worker = CameraWorker(mode, index, rtsp_url, read_zone=read_zone)
        self.worker.frame_ready.connect(self.frame_ready.emit)
        self.worker.status_changed.connect(self.status_changed.emit)
        self.worker.plate_detected.connect(self.plate_detected.emit)

    def start(self) -> None:
        if not self.worker.isRunning():
            self.worker.start()

    def stop(self) -> None:
        self.worker.stop()
        self.worker.wait(2000)

    def capture_current_frame(self):
        return self.worker.get_last_frame()

    def reset_recognizer(self) -> None:
        """Reset ALPR buffer between vehicles (call after each gate decision)."""
        self.worker.reset_recognizer()
