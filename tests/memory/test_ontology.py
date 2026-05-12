"""Ontology YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eidolon.memory.config.ontology import load_ontology


def test_load_default_ontology(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_ONTOLOGY_YAML", raising=False)
    ont = load_ontology()
    assert len(ont.wings) >= 1
    assert ont.mcp_tools.search_drawers


def test_duplicate_wing_ids_rejected(tmp_path: Path):
    bad = {
        "wings": [
            {"id": "W1", "display_name": "a"},
            {"id": "W1", "display_name": "b"},
        ],
        "mcp_tools": {},
    }
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_ontology(p)


def test_empty_wings_rejected(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text(yaml.safe_dump({"wings": [], "mcp_tools": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="at least one wing"):
        load_ontology(p)


def test_render_steward_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_ONTOLOGY_YAML", raising=False)
    ont = load_ontology()
    text = ont.render_steward_prompt()
    assert "Wing_" in text or "wing" in text.lower()
