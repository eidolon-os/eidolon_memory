"""McpRecallClient with fake backend."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.recall import McpRecallClient
from eidolon.memory.config.memory_settings import load_memory_settings


@pytest.mark.asyncio
async def test_recall_returns_hits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    fb = FakeMemoryBackend()
    await fb.ingest_text(wing="u", room="r", text="cat story", metadata=None)
    client = McpRecallClient(fb, settings)
    hits = await client.recall("cat", wing="u")
    assert hits
    assert "cat" in str(hits[0].value).lower() or "cat" in hits[0].key.lower()
