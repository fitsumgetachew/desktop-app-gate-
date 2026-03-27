from __future__ import annotations

import logging
import time
from typing import Optional

import cv2
from PySide6 import QtCore, QtGui

from smart_gate.utils.paths import get_detector_model_path

logger = logging.getLogger(__name__)


class CameraWorker(QtCore.QThread):
    frame_ready = QtCore.Signal(QtGui.QImage)
    status_changed = QtCore.Signal(bool, str)
    # plate, confidence, raw_text, ocr_confidence, crop (numpy array as object)
    plate_detected = QtCore.Signal(str, float, str, float, object)

    def __init__(self, mode: str, index: int, rtsp_url: str) -> None:
        super().__init__()
        self.mode = mode
        self.index = index
        self.rtsp_url = rtsp_url
        self._stop_flag = False
        self._last_frame = None
        self._recognizer = None

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

        while not self._stop_flag:
            cap = self._open_capture()
            if not cap:
                self.status_changed.emit(False, "Camera open failed")
                time.sleep(2)
                continue

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

                # Run ALPR pipeline on every frame
                if self._recognizer is not None:
                    try:
                        result = self._recognizer.process_frame(frame)
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

                image = self._to_qimage(frame)
                if image is not None:
                    self.frame_ready.emit(image)
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

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        if self.mode.upper() == "RTSP":
            if not self.rtsp_url:
                return None
            cap = cv2.VideoCapture(self.rtsp_url)
        else:
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

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

    def __init__(self, mode: str, index: int, rtsp_url: str) -> None:
        super().__init__()
        self.worker = CameraWorker(mode, index, rtsp_url)
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
