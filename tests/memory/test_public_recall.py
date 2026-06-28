"""Tests for shared MCP-style recall (``public_recall``)."""

from __future__ import annotations

import pytest
from eidolon_sdk.memory import MemoryActorContext, build_memory_actor_context

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import (
    group_recall_context,
    recall_record_visible_for_context,
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
)
from eidolon.memory.config.memory_settings import get_memory_settings

MEMORY_SPACE_ID = "default.alice.default"


def _context(owner_user_id: str = "alice") -> MemoryActorContext:
    return build_memory_actor_context(
        memory_realm_id=f"default.{owner_user_id}.default",
        owner_id=owner_user_id,
        companion_id="default",
        device_id="device",
        session_id="s1",
    )


@pytest.mark.asyncio
async def test_search_all_wings_matches_mcp_visibility():
    settings = get_memory_settings()
    profile = next(w for w in settings.wings if w.id == "Wing_Profile")
    backend = FakeMemoryBackend()

    await backend.ingest_text(
        wing=profile.id,
        room="profile_core",
        text="alice drinks tea in morning",
        metadata={"memory_space_id": MEMORY_SPACE_ID},
    )
    await backend.ingest_text(
        wing=profile.id,
        room="private_note",
        text="classified",
        metadata={"memory_space_id": MEMORY_SPACE_ID, "privacy": "private"},
    )

    out = await search_all_wings_mcp_style(
        backend,
        settings,
        query="tea",
        context=_context(),
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
        context=_context("bob"),
        top_k=5,
        wing=None,
        room=None,
    )
    assert out2 == []


def test_visible_filters_privacy_metadata():
    from eidolon.memory.domain.wire import MemoryWireRecord

    rec = MemoryWireRecord(
        memory_space_id=MEMORY_SPACE_ID,
        key="k",
        value="x",
        metadata={"memory_space_id": MEMORY_SPACE_ID},
    )
    assert recall_record_visible_for_context(rec, _context())
    priv = rec.model_copy(
        update={
            "metadata": {**rec.metadata, "privacy": "private"},
        }
    )
    assert not recall_record_visible_for_context(priv, _context())


@pytest.mark.asyncio
async def test_group_recall_context_non_empty_when_hits():
    from eidolon.memory.domain.wire import MemoryWireRecord

    hits = [
        MemoryWireRecord(
            memory_space_id=MEMORY_SPACE_ID,
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
        backend,
        query: str,
        wings: list[str],
        room: str | None,
        top_k: int,
        context: MemoryActorContext,
    ):
        del backend
        assert context.memory_space_id == MEMORY_SPACE_ID
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
        context=_context(),
        top_k=3,
        wing="Wing_Profile",
        room=None,
        for_voice=True,
        palace_path="/tmp/fake-palace",
    )
    assert calls == [["Wing_Profile"]]


@pytest.mark.asyncio
async def test_voice_shared_embedding_failure_degrades_to_empty(monkeypatch):
    """Chroma/mempalace pyo3 panics can arrive as BaseException wrappers.
    Voice recall must degrade instead of crashing the MCP worker process.
    """
    settings = get_memory_settings()
    backend = FakeMemoryBackend()

    class PanicLike(BaseException):
        pass

    async def _panic(*_args, **_kwargs):
        raise PanicLike("sqlite disk I/O panic")

    monkeypatch.setattr(
        "eidolon.memory.application.public_recall._search_voice_shared_embedding",
        _panic,
    )

    out = await search_all_wings_mcp_style(
        backend,
        settings,
        query="test",
        context=_context(),
        top_k=3,
        wing="Wing_Profile",
        room=None,
        for_voice=True,
        palace_path="/tmp/fake-palace",
    )
    assert out == []


@pytest.mark.asyncio
async def test_recall_fusion_marks_degraded_when_voice_fast_path_fails(monkeypatch):
    settings = get_memory_settings()
    backend = FakeMemoryBackend()

    class PanicLike(BaseException):
        pass

    async def _panic(*_args, **_kwargs):
        raise PanicLike("sqlite disk I/O panic")

    monkeypatch.setattr(
        "eidolon.memory.application.public_recall._search_voice_shared_embedding",
        _panic,
    )

    result = await recall_with_kg_fusion(
        backend,
        settings,
        query="test",
        context=_context(),
        top_k=3,
        kg=None,
        for_voice=True,
        palace_path="/tmp/fake-palace",
    )
    assert result["vector"] == []
    assert result["degraded"] is True
