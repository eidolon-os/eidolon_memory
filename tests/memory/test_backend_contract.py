"""Backend contract behavior for the MemPalace Python adapter."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.mempalace_python_backend import (
    MemPalacePythonBackend,
    _drawer_id,
    _metadata_for_chroma,
)
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.errors import MemoryBackendUnsupported


def test_drawer_id_matches_mempalace_deterministic_shape():
    did = _drawer_id("Wing_Work", "project_x", "hello")
    assert did.startswith("drawer_Wing_Work_project_x_")
    assert len(did.rsplit("_", 1)[-1]) == 24


def test_metadata_for_chroma_serializes_nested_values():
    meta = _metadata_for_chroma({"tags": ["a", "b"], "importance": 4, "empty": None})
    assert meta["tags"] == '["a", "b"]'
    assert meta["importance"] == 4
    assert "empty" not in meta


@pytest.mark.asyncio
async def test_delete_requires_drawer_id_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    backend = MemPalacePythonBackend(load_memory_settings(), "/tmp/palace")
    with pytest.raises(MemoryBackendUnsupported):
        await backend.delete("u", "not-a-drawer-id")
