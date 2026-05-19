"""Per-user palace resolution (D1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.config.palace_directory import (
    resolve_palace_for_user,
    resolve_palaces_root,
    validate_user_id,
)


def _write_settings(tmp_path: Path, palaces_root: str = "") -> Path:
    data = {
        "wings": [{"id": "Wing_Profile", "display_name": "p"}],
        "runtime": {"palaces_root": palaces_root},
    }
    p = tmp_path / "memory.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_resolve_palaces_root_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_PALACES_ROOT", raising=False)
    settings = load_memory_settings(_write_settings(tmp_path))
    assert resolve_palaces_root(settings) == (Path.home() / "eidolon" / "palaces").resolve()


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


def test_resolve_palace_for_user_joins_root_and_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EIDOLON_MEMORY_PALACES_ROOT", str(tmp_path))
    settings = load_memory_settings(_write_settings(tmp_path))
    assert resolve_palace_for_user(settings, "alice") == (tmp_path / "alice").resolve()


def test_resolve_palace_for_user_path_override(tmp_path: Path) -> None:
    settings = load_memory_settings(_write_settings(tmp_path))
    p = resolve_palace_for_user(settings, "alice", path_override="/tmp/probe")
    assert p == Path("/tmp/probe").resolve()


def test_validate_user_id_rejects_path_separators() -> None:
    with pytest.raises(ValueError):
        validate_user_id("../escape")
    with pytest.raises(ValueError):
        validate_user_id("alice/bob")


def test_validate_user_id_accepts_safe_chars() -> None:
    assert validate_user_id("alice") == "alice"
    assert validate_user_id("alice_123-test.dev") == "alice_123-test.dev"
