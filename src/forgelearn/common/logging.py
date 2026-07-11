"""Shared logging setup for ForgeLearn.

All library code obtains its logger via :func:`get_logger` — never via bare
``print()``. The root ``forgelearn`` logger is configured exactly once, using the
level from central config, so every module logs with a consistent format.
"""

from __future__ import annotations

import logging

from forgelearn.config import get_settings

_ROOT_LOGGER_NAME = "forgelearn"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _configure_root() -> None:
    """Attach a single stream handler to the root ForgeLearn logger once."""
    global _configured
    if _configured:
        return

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(get_settings().log_level)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    # Don't double-log through the Python root logger.
    logger.propagate = False

    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a configured logger under the ``forgelearn`` namespace.

    Args:
        name: Optional dotted suffix for the module (e.g. ``"config"``). When
            omitted, the root ``forgelearn`` logger is returned.

    Returns:
        A ready-to-use :class:`logging.Logger`.
    """
    _configure_root()
    if name:
        return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
    return logging.getLogger(_ROOT_LOGGER_NAME)
