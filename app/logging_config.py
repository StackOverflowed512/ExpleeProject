"""Logging configuration.

Only configuration is defined here. Application code uses standard library
loggers (``logging.getLogger(__name__)``) and must never log credentials,
tokens, or full connection URLs containing passwords.
"""

from __future__ import annotations

import logging
from typing import Final

_LOG_FORMAT: Final = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_HANDLER_MARKER: Final = "_explee_handler"


def configure_logging(level: str) -> None:
    """Configure a single stderr handler on the root logger.

    Reuses an existing handler installed by this function so repeated app
    construction (for example in tests) does not duplicate log lines.
    """
    root = logging.getLogger()

    if not any(getattr(handler, _HANDLER_MARKER, False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)

    root.setLevel(level.upper())
