"""
logsetup.py â€” one rotating file log per process, shared by api.py and live_runner.py.

Production rule: console stays human-readable (uvicorn keeps its own access
logs), while everything this codebase logs goes to data/logs/<name>.log with
rotation so a long-running live runner can never grow an unbounded file.

Env overrides:
    LOG_LEVEL   DEBUG | INFO | WARNING | ERROR   (default INFO)
    LOG_DIR     directory for the .log files     (default <root>/data/logs)
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from tbb import config as paths

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(paths.DATA_DIR, "logs"))

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """
    Named logger writing to LOG_DIR/<name>.log (5 MB x 3 rotations).

    Idempotent: repeated calls return the same configured logger instead of
    stacking handlers, so importing modules can call this freely.
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_rotating_configured", False):
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, f"{name}.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False
    logger._rotating_configured = True
    return logger
