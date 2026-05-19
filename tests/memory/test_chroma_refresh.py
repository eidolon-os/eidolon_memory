"""Chroma/SQLite error classification."""

from __future__ import annotations

from eidolon.memory.infrastructure.chroma_refresh import (
    is_disk_io_error,
    is_recoverable_db_error,
)


def test_is_disk_io_error_matches_chroma_522():
    exc = Exception(
        "Database error: error returned from database: (code: 522) disk I/O error"
    )
    assert is_disk_io_error(exc)
    assert is_recoverable_db_error(exc)
