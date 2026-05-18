"""Tests for palace generation atomic file."""

from __future__ import annotations

import json
from pathlib import Path

from eidolon.memory.infrastructure.palace_generation import (
    bump_generation,
    read_generation,
    resolve_generation_path,
)


def test_bump_generation_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "gen.json"
    a = bump_generation(path)
    b = bump_generation(path)
    assert b.generation == a.generation + 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["generation"] == b.generation


def test_read_generation_missing(tmp_path: Path) -> None:
    info = read_generation(tmp_path / "missing.json")
    assert info.generation == 0


def test_resolve_generation_path_default(tmp_path: Path) -> None:
    p = resolve_generation_path(tmp_path)
    assert p.name == "palace_generation"
    assert ".eidolon" in str(p)
