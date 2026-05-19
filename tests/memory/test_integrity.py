"""D2/D4 守门 helpers: integrity_check + location guard + fsync_directory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.integrity import (
    PalaceLocationError,
    assert_palace_location_safe,
    fsync_directory,
    run_integrity_check,
)


def _make_db(tmp_path: Path) -> Path:
    p = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    return p


def test_integrity_check_ok(tmp_path: Path) -> None:
    p = _make_db(tmp_path)
    report = run_integrity_check(str(p))
    assert report.ok
    assert report.detail == "ok"
    assert report.pragma == "integrity_check"


def test_quick_check_ok(tmp_path: Path) -> None:
    p = _make_db(tmp_path)
    report = run_integrity_check(str(p), quick=True)
    assert report.ok
    assert report.pragma == "quick_check"


def test_integrity_check_missing_file(tmp_path: Path) -> None:
    report = run_integrity_check(str(tmp_path / "nope.sqlite3"))
    assert not report.ok
    assert report.detail == "missing"


def test_assert_palace_location_safe_accepts_local_path(tmp_path: Path) -> None:
    assert_palace_location_safe(tmp_path)


def test_assert_palace_location_safe_rejects_icloud_path() -> None:
    with pytest.raises(PalaceLocationError):
        assert_palace_location_safe(
            "/Users/test/Library/Mobile Documents/com~apple~CloudDocs/eidolon"
        )


def test_assert_palace_location_safe_rejects_dropbox_path() -> None:
    with pytest.raises(PalaceLocationError):
        assert_palace_location_safe("/Users/test/Dropbox/eidolon")


def test_fsync_directory_noop_on_missing(tmp_path: Path) -> None:
    # Should not raise — best-effort
    fsync_directory(tmp_path / "missing-subdir")


def test_fsync_directory_on_real_dir(tmp_path: Path) -> None:
    fsync_directory(tmp_path)
