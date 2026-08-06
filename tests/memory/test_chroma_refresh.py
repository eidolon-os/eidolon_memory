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


def _wal_database(tmp_path: Path) -> Path:
    p = _make_sqlite(tmp_path)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    return p


@pytest.mark.parametrize("mode", ["PASSIVE", "TRUNCATE"])
def test_checkpoint_sqlite_wal_runs(tmp_path: Path, mode: str) -> None:
    p = _wal_database(tmp_path)
    # Should not raise
    checkpoint_sqlite_wal(str(p), mode=mode)


def test_a_checkpoint_reports_the_pages_it_moved(tmp_path: Path) -> None:
    """The numbers exist, because the whole point is telling apart two successes."""

    p = _wal_database(tmp_path)
    conn = sqlite3.connect(str(p))
    conn.executemany("INSERT INTO t(x) VALUES (?)", [(i,) for i in range(500)])
    conn.commit()

    # Measured with the writer still open: SQLite checkpoints on the last
    # connection closing, so closing first leaves an empty log and a test that
    # asserts nothing.
    result = checkpoint_sqlite_wal(str(p))
    conn.close()

    assert result.ran
    assert not result.busy
    assert result.wal_pages > 0
    assert result.checkpointed_pages == result.wal_pages
    assert not result.stalled


def test_an_open_read_cursor_stalls_the_checkpoint_without_failing_it(
    tmp_path: Path,
) -> None:
    """The failure this return value exists for.

    A checkpoint cannot move past any live reader's snapshot. The pragma reports
    no error for this — it reports pages in the log and zero moved — so before
    the result was returned, a WAL growing without bound behind a stuck reader
    looked exactly like a healthy idle service.
    """

    p = _wal_database(tmp_path)
    writer = sqlite3.connect(str(p))
    writer.executemany("INSERT INTO t(x) VALUES (?)", [(i,) for i in range(500)])
    writer.commit()
    # Empty the log first, so the reader's snapshot sits at its start. A reader
    # that arrives mid-log only blocks the frames after its own mark, which is a
    # partial stall and a weaker assertion.
    checkpoint_sqlite_wal(str(p), mode="TRUNCATE")

    # Held open deliberately, and bound to a name: an unreferenced cursor is
    # reset by refcounting the moment it goes out of scope, which is why the
    # production alias loop gets away with the same shape.
    reader = sqlite3.connect(str(p))
    cursor = reader.execute("SELECT x FROM t")
    cursor.fetchone()

    writer.executemany("INSERT INTO t(x) VALUES (?)", [(i,) for i in range(500)])
    writer.commit()

    stalled = checkpoint_sqlite_wal(str(p))

    assert stalled.ran
    assert stalled.wal_pages > 0
    assert stalled.checkpointed_pages == 0
    assert stalled.stalled, "a pinned log must be distinguishable from a quiet one"

    # Draining the cursor is what releases it — not closing the connections,
    # which would auto-checkpoint on the way out and prove nothing.
    cursor.fetchall()

    recovered = checkpoint_sqlite_wal(str(p))
    assert recovered.checkpointed_pages > 0
    assert not recovered.stalled

    reader.close()
    writer.close()


def test_a_missing_file_is_not_reported_as_a_stall(tmp_path: Path) -> None:
    result = checkpoint_sqlite_wal(str(tmp_path / "absent.sqlite3"))

    assert not result.ran
    assert not result.stalled


def test_a_database_without_wal_reports_no_negative_progress(tmp_path: Path) -> None:
    """SQLite answers ``-1`` for both counts here, which is not lost pages."""

    p = _make_sqlite(tmp_path)

    result = checkpoint_sqlite_wal(str(p))

    assert result.ran
    assert result.wal_pages == 0
    assert result.checkpointed_pages == 0
    assert not result.stalled
