"""Steward prompt rendering."""

from __future__ import annotations

import pytest

from eidolon.memory.config.memory_settings import load_memory_settings


def test_prompt_contains_wings_schema_and_privacy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    text = load_memory_settings().render_steward_prompt()
    assert "Wing_Profile" in text
    assert '"privacy_actions"' in text
    assert "不要记住" in text
    assert "只输出 JSON" in text
