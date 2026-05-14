"""Memory settings YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.memory_settings import (
    get_memory_settings,
    load_memory_settings,
)


def test_load_default_memory_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    assert len(settings.wings) >= 1
    assert settings.nats.subject


def test_get_memory_settings_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    a = get_memory_settings()
    b = get_memory_settings()
    assert a is b


def test_duplicate_wing_ids_rejected(tmp_path: Path):
    bad = {
        "wings": [
            {"id": "W1", "display_name": "a"},
            {"id": "W1", "display_name": "b"},
        ],
        "nats": {},
    }
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_memory_settings(p)


def test_empty_wings_rejected(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text(yaml.safe_dump({"wings": [], "nats": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="at least one wing"):
        load_memory_settings(p)


def test_render_steward_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    text = settings.render_steward_prompt()
    assert "Wing_" in text or "wing" in text.lower()
