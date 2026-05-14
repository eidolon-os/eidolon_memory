"""Lightweight logging for ``eidolon.memory`` — avoids hard dependency on agent logging."""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("eidolon.memory")


class _KeywordLogger:
    """Tiny adapter that accepts structlog-style keyword fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.debug(_format(msg, kwargs), *args)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.info(_format(msg, kwargs), *args)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(_format(msg, kwargs), *args)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.error(_format(msg, kwargs), *args)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._logger.exception(_format(msg, kwargs), *args)


def _format(msg: str, fields: dict[str, Any]) -> str:
    if not fields:
        return msg
    suffix = " ".join(f"{key}={value!r}" for key, value in fields.items())
    return f"{msg} {suffix}"


def get_logger(name: str) -> Any:
    """Return a stdlib logger; structlog optional hook can be added later."""
    return _KeywordLogger(logging.getLogger(name))
