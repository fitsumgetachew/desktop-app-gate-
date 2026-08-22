"""Set the camera password into .env safely.

Run from the repo root:  .venv/bin/python scripts/set_camera_password.py

Prompts for the camera's admin password (input is hidden, nothing is echoed),
URL-encodes special characters automatically (@ : / # etc.), and rewrites the
CAMERA_RTSP_URL line in .env. You never have to hand-encode anything.
"""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path
from urllib.parse import quote

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_IP = "192.168.1.64"
DEFAULT_CHANNEL = "102"  # sub-stream — right choice for ALPR


def main() -> int:
    if not ENV_PATH.exists():
        print(f"FAIL: {ENV_PATH} not found")
        return 1

    ip = input(f"Camera IP [{DEFAULT_IP}]: ").strip() or DEFAULT_IP
    user = input("Camera username [admin]: ").strip() or "admin"
    pw = getpass.getpass("Camera password (hidden): ")
    if not pw:
        print("FAIL: empty password")
        return 1
    pw2 = getpass.getpass("Repeat password: ")
    if pw != pw2:
        print("FAIL: passwords do not match")
        return 1

    encoded = quote(pw, safe="")
    url = f"rtsp://{user}:{encoded}@{ip}:554/Streaming/Channels/{DEFAULT_CHANNEL}"

    text = ENV_PATH.read_text()
    new_text, n = re.subn(r"(?m)^CAMERA_RTSP_URL=.*$", f"CAMERA_RTSP_URL={url}", text)
    if n == 0:
        new_text = text.rstrip() + f"\nCAMERA_RTSP_URL={url}\n"
    new_text, _ = re.subn(r"(?m)^CAMERA_MODE=.*$", "CAMERA_MODE=RTSP", new_text)
    ENV_PATH.write_text(new_text)

    masked = f"rtsp://{user}:****@{ip}:554/Streaming/Channels/{DEFAULT_CHANNEL}"
    print(f"OK: wrote {masked} to .env ({n or 1} line updated)")
    print("Now run:  .venv/bin/python scripts/test_camera.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
