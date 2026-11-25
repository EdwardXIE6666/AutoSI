"""Console (colored) + rotating file logging."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_COLORS = {
    logging.DEBUG: "\033[1;30m",
    logging.INFO: "\033[1;34m",
    logging.WARNING: "\033[1;33m",
    logging.ERROR: "\033[1;31m",
    logging.CRITICAL: "\033[1;35m",
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{_RESET}" if color else message


def build_logger(log_file: str, name: str = "siwave_extractor",
                 level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:           # idempotent: don't stack handlers on re-call
        return logger

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    console = logging.StreamHandler()
    console.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s",
                                        datefmt="%H:%M:%S"))
    logger.addHandler(console)

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3,
                                       encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(file_handler)
    return logger
