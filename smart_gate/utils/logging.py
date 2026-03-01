from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from smart_gate.utils.paths import get_logs_dir


def setup_logging(level: int = logging.INFO) -> None:
    log_dir = get_logs_dir()
    log_file = Path(log_dir) / "app.log"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
