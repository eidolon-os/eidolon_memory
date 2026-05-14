"""Recall policy filtering (taboo / top_k)."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.mempalace_python_backend import apply_recall_policy
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.wire import MemoryWireRecord


def test_taboo_filtered(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    hits = [
        MemoryWireRecord(user_id="a", key="b", value="keep", metadata={"room_status": "active"}),
        MemoryWireRecord(user_id="a", key="c", value="gone", metadata={"room_status": "taboo"}),
    ]
    out = apply_recall_policy(hits, settings)
    assert len(out) == 1
    assert out[0].value == "keep"


def test_top_k_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    hits = [
        MemoryWireRecord(user_id="a", key=str(i), value=str(i), metadata={})
        for i in range(20)
    ]
    out = apply_recall_policy(hits, settings)
    assert len(out) <= settings.recall.top_k
