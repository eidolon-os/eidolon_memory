"""WAL pragma helpers (D1: simplified module, no error-classification helpers)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.chroma_refresh import (
    checkpoint_sqlite_wal,
    ensure_sqlite_wal,
)


def _make_sqlite(tmp_path: Path) -> Path:
    p = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    return p


def test_ensure_sqlite_wal_sets_mode_and_synchronous(tmp_path: Path) -> None:
    p = _make_sqlite(tmp_path)
    info = ensure_sqlite_wal(str(p), synchronous="FULL")
    assert info["journal_mode"].lower() == "wal"
    assert info["synchronous"] == "FULL"


def test_ensure_sqlite_wal_missing_file(tmp_path: Path) -> None:
    info = ensure_sqlite_wal(str(tmp_path / "nope.sqlite3"))
    assert info["journal_mode"] == "missing"


@pytest.mark.parametrize("mode", ["PASSIVE", "TRUNCATE"])
def test_checkpoint_sqlite_wal_runs(tmp_path: Path, mode: str) -> None:
    p = _make_sqlite(tmp_path)
    ensure_sqlite_wal(str(p))
    # Should not raise
    checkpoint_sqlite_wal(str(p), mode=mode)
