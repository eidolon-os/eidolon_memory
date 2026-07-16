"""Checkpoint helper for Eidolon-owned SQLite databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.chroma_refresh import (
    checkpoint_sqlite_wal,
)


def _make_sqlite(tmp_path: Path) -> Path:
    p = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    return p


def test_backend_construction_does_not_mutate_chroma_journal_mode(tmp_path: Path) -> None:
    sqlite_path = _make_sqlite(tmp_path)
    with sqlite3.connect(sqlite_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)

    settings = MemorySettings()
    settings.chromadb.synchronous = "OFF"  # legacy config must remain inert
    MemPalacePythonBackend(settings, str(tmp_path), memory_space_id="realm-a")

    with sqlite3.connect(sqlite_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)


@pytest.mark.parametrize("mode", ["PASSIVE", "TRUNCATE"])
def test_checkpoint_sqlite_wal_runs(tmp_path: Path, mode: str) -> None:
    p = _make_sqlite(tmp_path)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    # Should not raise
    checkpoint_sqlite_wal(str(p), mode=mode)
