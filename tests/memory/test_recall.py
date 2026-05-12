"""McpRecallClient with fake backend."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.recall import McpRecallClient
from eidolon.memory.config.ontology import load_ontology


@pytest.mark.asyncio
async def test_recall_returns_hits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_ONTOLOGY_YAML", raising=False)
    ont = load_ontology()
    fb = FakeMemoryBackend()
    await fb.ingest_text(wing="u", room="r", text="cat story", metadata=None)
    client = McpRecallClient(fb, ont)
    hits = await client.recall("cat", wing="u")
    assert hits
    assert "cat" in str(hits[0].value).lower() or "cat" in hits[0].key.lower()
