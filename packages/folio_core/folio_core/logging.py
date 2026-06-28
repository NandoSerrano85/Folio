"""Structured logging configuration honoring ``LOG_LEVEL``.

Emits single-line, key-friendly records to stdout (the right thing inside
containers). Call :func:`configure_logging` once at process start; use
:func:`get_logger` everywhere else.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger once. Idempotent.

    ``level`` overrides the configured ``LOG_LEVEL`` when provided.
    """
    global _CONFIGURED

    if level is None:
        # Imported lazily so logging has no hard import cycle with config.
        from folio_core.config import get_settings

        level = get_settings().log_level

    root = logging.getLogger()
    root.setLevel(level)

    if not _CONFIGURED:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT))
        root.addHandler(handler)
        # Tame chatty third-party libraries.
        logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
        _CONFIGURED = True
    else:
        for handler in root.handlers:
            handler.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring logging has been configured."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


__all__ = ["configure_logging", "get_logger"]
