from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler


def setup_logger(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        console = Console()
        rich_handler = RichHandler(console=console, rich_tracebacks=True, show_time=True, show_level=True)
        rich_handler.setLevel(logging.INFO)

        logfile = logs_dir / "agent.log"
        file_handler = RotatingFileHandler(logfile, maxBytes=5_000_000, backupCount=3)
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)

        logger.addHandler(rich_handler)
        logger.addHandler(file_handler)

    return logger


