"""Rotating file logging setup, per the design's explicit requirement.
Call configure_logging() once, at startup, before anything else logs."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "bot.log"

    root = logging.getLogger()
    root.setLevel(level)
    # Clear any handlers a prior call (e.g. in tests) may have left attached,
    # so configure_logging is safe to call more than once without doubling
    # every log line.
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # httpx/telegram libraries are chatty at INFO - keep them at WARNING
    # unless the whole app is explicitly set to DEBUG.
    if level.upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.WARNING)
