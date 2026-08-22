"""Field test for the gate camera connection.

Run from the repo root:  .venv/bin/python scripts/test_camera.py

Reads CAMERA_MODE / CAMERA_RTSP_URL / CAMERA_INDEX from the same config the
app uses, opens the stream through the app's own capture path, and reports
resolution, frame rate, and codec. Saves a snapshot to /tmp/camera_test.jpg
so you can eyeball what the camera sees. Never prints the password.
"""
from __future__ import annotations

import re
import sys
import time

sys.path.insert(0, ".")

import cv2  # noqa: E402

from smart_gate.utils.config import load_config  # noqa: E402
from smart_gate.services.camera_service import CameraWorker  # noqa: E402


def mask(url: str) -> str:
    return re.sub(r"//([^:/@]+):[^@]+@", r"//\1:****@", url)


def main() -> int:
    cfg = load_config()
    print(f"mode: {cfg.camera_mode}")
    if cfg.camera_mode.upper() == "RTSP":
        print(f"url:  {mask(cfg.camera_rtsp_url)}")
        if "YOUR_PASSWORD" in cfg.camera_rtsp_url:
            print("FAIL: .env still contains the YOUR_PASSWORD placeholder.")
            print("      Edit CAMERA_RTSP_URL in .env and SAVE the file.")
            return 1
    else:
        print(f"index: {cfg.camera_index}")

    worker = CameraWorker(cfg.camera_mode, cfg.camera_index, cfg.camera_rtsp_url)
    t0 = time.monotonic()
    cap = worker._open_capture()
    dt = time.monotonic() - t0
    if cap is None:
        print(f"FAIL: stream did not open ({dt:.1f}s).")
        print("      Wrong password (check %40 encoding for '@'), wrong IP, or camera off.")
        return 1
    print(f"OK: stream opened in {dt:.1f}s")

    frames, first = 0, None
    t1 = time.monotonic()
    while time.monotonic() - t1 < 5:
        ret, frame = cap.read()
        if ret:
            frames += 1
            if first is None:
                first = frame
    fps = frames / 5.0
    if first is None:
        print("FAIL: stream opened but no frames decoded (codec issue? set sub-stream to H.264)")
        cap.release()
        return 1

    h, w = first.shape[:2]
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4))
    print(f"OK: {w}x{h} @ {fps:.1f} fps, codec {codec}")
    snap = "/tmp/camera_test.jpg"
    cv2.imwrite(snap, first)
    print(f"OK: snapshot saved to {snap} — open it to see the camera view")

    if fps < 10:
        print("WARN: fps is low — check sub-stream settings (1080p, 20-25 fps, H.264)")
    if w > 1920:
        print("WARN: this looks like the 8MP main stream — use /Streaming/Channels/102")
    cap.release()
    print("ALL GOOD — run the app:  python -m smart_gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
