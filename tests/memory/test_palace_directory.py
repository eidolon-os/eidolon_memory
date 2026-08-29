"""Per-memory-space palace resolution (D1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.config.palace_directory import (
    PALACE_STORAGE_EPOCH,
    memory_space_storage_name,
    resolve_palace_for_memory_space,
    resolve_palaces_root,
    validate_memory_space_id,
)


def _write_settings(tmp_path: Path, palaces_root: str = "") -> Path:
    data = {
        "runtime": {"palaces_root": palaces_root},
    }
    p = tmp_path / "memory.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_resolve_palaces_root_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_PALACES_ROOT", raising=False)
    settings = load_memory_settings(_write_settings(tmp_path))
    assert (
        resolve_palaces_root(settings)
        == (Path.home() / "eidolon" / "data" / "memory" / PALACE_STORAGE_EPOCH).resolve()
    )


def test_resolve_palaces_root_config_wins_over_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_PALACES_ROOT", raising=False)
    settings = load_memory_settings(_write_settings(tmp_path, palaces_root="/tmp/cfg-palaces"))
    assert resolve_palaces_root(settings) == Path("/tmp/cfg-palaces").resolve()


def test_resolve_palaces_root_env_wins_over_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_memory_settings(_write_settings(tmp_path, palaces_root="/tmp/cfg-palaces"))
    monkeypatch.setenv("EIDOLON_MEMORY_PALACES_ROOT", "/tmp/env-palaces")
    assert resolve_palaces_root(settings) == Path("/tmp/env-palaces").resolve()


def test_resolve_palace_for_memory_space_joins_root_and_storage_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EIDOLON_MEMORY_PALACES_ROOT", str(tmp_path))
    settings = load_memory_settings(_write_settings(tmp_path))
    storage_name = memory_space_storage_name("r:alice:default")
    assert "/" not in storage_name
    assert ":" not in storage_name
    assert (
        resolve_palace_for_memory_space(settings, "r:alice:default")
        == (tmp_path / storage_name).resolve()
    )


def test_resolve_palace_for_memory_space_path_override(tmp_path: Path) -> None:
    settings = load_memory_settings(_write_settings(tmp_path))
    p = resolve_palace_for_memory_space(settings, "default.alice.mochi", path_override="/tmp/probe")
    assert p == Path("/tmp/probe").resolve()


def test_validate_memory_space_id_rejects_path_separators() -> None:
    with pytest.raises(ValueError):
        validate_memory_space_id("../escape")
    with pytest.raises(ValueError):
        validate_memory_space_id("default.alice/bob.mochi")


def test_validate_memory_space_id_accepts_safe_chars() -> None:
    assert validate_memory_space_id("default.alice_123.mochi-test") == (
        "default.alice_123.mochi-test"
    )
