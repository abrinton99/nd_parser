"""Logging setup (spec §13): plain-text file rotated daily (14 days kept),
mirrored to stdout unless --quiet."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGGER_NAME = "nextdoor_watcher"


class _UTCFormatter(logging.Formatter):
    """Emits the leading UTC timestamp the spec's log lines expect:
    `2026-05-21T14:30:05Z <message>`."""

    def format(self, record: logging.LogRecord) -> str:
        from .util import iso
        prefix = iso()
        level = "" if record.levelno <= logging.INFO else f"{record.levelname} "
        return f"{prefix} {level}{record.getMessage()}"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(log_file: str | Path | None, *, quiet: bool = False,
                  level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    fmt = _UTCFormatter()

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            str(path), when="midnight", backupCount=14, utc=True, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    if not quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if not logger.handlers:  # quiet + no file: avoid "no handlers" warnings
        logger.addHandler(logging.NullHandler())

    return logger
