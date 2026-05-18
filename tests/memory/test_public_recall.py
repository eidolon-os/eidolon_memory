"""Tests for shared MCP-style recall (``public_recall``)."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import (
    group_recall_context,
    recall_record_visible_for_user,
    search_all_wings_mcp_style,
)
from eidolon.memory.config.memory_settings import get_memory_settings


@pytest.mark.asyncio
async def test_search_all_wings_matches_mcp_visibility():
    settings = get_memory_settings()
    profile = next(w for w in settings.wings if w.id == "Wing_Profile")
    backend = FakeMemoryBackend()

    await backend.ingest_text(
        wing=profile.id,
        room="profile_core",
        text="alice drinks tea in morning",
        metadata={"user_id": "alice"},
    )
    await backend.ingest_text(
        wing=profile.id,
        room="private_note",
        text="classified",
        metadata={"user_id": "alice", "privacy": "private"},
    )

    out = await search_all_wings_mcp_style(
        backend,
        settings,
        query="tea",
        user_id="alice",
        top_k=5,
        wing=None,
        room=None,
    )
    assert len(out) == 1
    assert "tea" in str(out[0].value)

    # Other user's fragment should stay hidden via metadata gate
    out2 = await search_all_wings_mcp_style(
        backend,
        settings,
        query="tea",
        user_id="bob",
        top_k=5,
        wing=None,
        room=None,
    )
    assert out2 == []


def test_visible_filters_privacy_metadata():
    from eidolon.memory.domain.wire import MemoryWireRecord

    rec = MemoryWireRecord(
        user_id="Wing_Profile",
        key="k",
        value="x",
        metadata={"user_id": "u1"},
    )
    assert recall_record_visible_for_user(rec, "u1")
    priv = rec.model_copy(
        update={
            "metadata": {**rec.metadata, "privacy": "private"},
        }
    )
    assert not recall_record_visible_for_user(priv, "u1")


@pytest.mark.asyncio
async def test_group_recall_context_non_empty_when_hits():
    from eidolon.memory.domain.wire import MemoryWireRecord

    hits = [
        MemoryWireRecord(
            user_id="Wing_Event",
            key="ev",
            value="travel to sea",
            metadata={"memory_type": "event"},
        )
    ]
    ctx = group_recall_context(hits)
    assert "事件" in ctx or "生活" in ctx


@pytest.mark.asyncio
async def test_single_wing_voice_uses_shared_embedding_path(monkeypatch):
    """Single wing + palace_path should use fast path when for_voice=True."""
    settings = get_memory_settings()
    backend = FakeMemoryBackend()
    calls: list[list[str]] = []

    async def _fake_shared(
        palace_path: str,
        _settings,
        *,
        query: str,
        wings: list[str],
        room: str | None,
        top_k: int,
        user_id: str,
    ):
        calls.append(list(wings))
        return []

    monkeypatch.setattr(
        "eidolon.memory.application.public_recall._search_voice_shared_embedding",
        _fake_shared,
    )
    await search_all_wings_mcp_style(
        backend,
        settings,
        query="test",
        user_id="alice",
        top_k=3,
        wing="Wing_Profile",
        room=None,
        for_voice=True,
        palace_path="/tmp/fake-palace",
    )
    assert calls == [["Wing_Profile"]]
