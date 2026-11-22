"""Console and file logging for the NordicCart command-line tools."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def setup_logging(log_dir: Path, run_name: str, level: str = "INFO") -> Path:
    """Log to the console (chosen level) and to logs/<run_name>_<timestamp>.log (DEBUG)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{run_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    root = logging.getLogger("nordiccart")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(FORMAT, "%H:%M:%S"))
    root.addHandler(console)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_handler)
    return path
