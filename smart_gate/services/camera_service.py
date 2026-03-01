from __future__ import annotations

import logging
import time
from typing import Optional

import cv2
from PySide6 import QtCore, QtGui

logger = logging.getLogger(__name__)


class CameraWorker(QtCore.QThread):
    frame_ready = QtCore.Signal(QtGui.QImage)
    status_changed = QtCore.Signal(bool, str)

    def __init__(self, mode: str, index: int, rtsp_url: str) -> None:
        super().__init__()
        self.mode = mode
        self.index = index
        self.rtsp_url = rtsp_url
        self._stop_flag = False
        self._last_frame = None

    def stop(self) -> None:
        self._stop_flag = True

    def get_last_frame(self):
        return self._last_frame

    def run(self) -> None:
        while not self._stop_flag:
            cap = self._open_capture()
            if not cap:
                self.status_changed.emit(False, "Camera open failed")
                time.sleep(2)
                continue

            self.status_changed.emit(True, "Camera connected")
            while not self._stop_flag:
                ret, frame = cap.read()
                if not ret:
                    self.status_changed.emit(False, "Camera disconnected")
                    break
                self._last_frame = frame
                image = self._to_qimage(frame)
                if image is not None:
                    self.frame_ready.emit(image)
                time.sleep(0.03)
            cap.release()
            time.sleep(1)

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

    def __init__(self, mode: str, index: int, rtsp_url: str) -> None:
        super().__init__()
        self.worker = CameraWorker(mode, index, rtsp_url)
        self.worker.frame_ready.connect(self.frame_ready.emit)
        self.worker.status_changed.connect(self.status_changed.emit)

    def start(self) -> None:
        if not self.worker.isRunning():
            self.worker.start()

    def stop(self) -> None:
        self.worker.stop()
        self.worker.wait(2000)

    def capture_current_frame(self):
        return self.worker.get_last_frame()
