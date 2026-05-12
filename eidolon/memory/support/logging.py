"""Lightweight logging for ``eidolon.memory`` — avoids hard dependency on agent logging."""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("eidolon.memory")


def get_logger(name: str) -> Any:
    """Return a stdlib logger; structlog optional hook can be added later."""
    return logging.getLogger(name)
