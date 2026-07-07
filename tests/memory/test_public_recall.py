"""Tests for shared MCP-style recall (``public_recall``)."""

from __future__ import annotations

import pytest
from eidolon_sdk.memory import MemoryActorContext, build_memory_actor_context

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import (
    recall_record_visible_for_context,
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
)
from eidolon.memory.application.recall_renderer import group_recall_context
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


@pytest.mark.asyncio
async def test_search_exact_fallback_recalls_short_name_fact_when_vector_empty():
    settings = get_memory_settings()

    class EmptyVectorBackend(FakeMemoryBackend):
        async def search(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.searches.append((args, kwargs))
            return []

    backend = EmptyVectorBackend()
    await backend.ingest_text(
        wing="Wing_Profile",
        room="profile_core",
        text="用户的名字是曼森。",
        metadata={
            "memory_space_id": MEMORY_SPACE_ID,
            "scope": "persona",
            "visibility": "all_devices",
        },
    )

    out = await search_all_wings_mcp_style(
        backend,
        settings,
        query="我叫什么名字",
        context=_context(),
        top_k=5,
        wing=None,
        room=None,
    )

    assert len(out) == 1
    assert out[0].value == "用户的名字是曼森。"
    assert out[0].metadata["retrieval"] == "lexical_fallback"


@pytest.mark.asyncio
async def test_search_exact_fallback_survives_vector_backend_error():
    settings = get_memory_settings()

    class BrokenVectorBackend(FakeMemoryBackend):
        async def search(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("vector index unavailable")

    backend = BrokenVectorBackend()
    await backend.ingest_text(
        wing="Wing_Profile",
        room="profile_core",
        text="用户的名字是曼森。",
        metadata={
            "memory_space_id": MEMORY_SPACE_ID,
            "scope": "persona",
            "visibility": "all_devices",
        },
    )

    out = await search_all_wings_mcp_style(
        backend,
        settings,
        query="曼森",
        context=_context(),
        top_k=5,
        wing="Wing_Profile",
        room="profile_core",
    )

    assert [r.value for r in out] == ["用户的名字是曼森。"]
    assert out[0].metadata["retrieval"] == "lexical_fallback"


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


def test_parse_search_payload_stamps_default_memory_space_id():
    """Root fix: vector hits lose their memory_space_id (mempalace drops custom
    metadata), so the caller-supplied authoritative id must be used instead of
    the bogus wing-name fallback that would fail recall's visibility gate."""
    from eidolon.memory.adapters.search_payload import parse_search_tool_payload

    # A vector hit as mempalace returns it: wing/room/text, NO memory_space_id.
    stripped = {"results": [{"wing": "Wing_Life", "room": "r1", "text": "plain drawer"}]}
    assert parse_search_tool_payload(
        stripped, default_memory_space_id="realm-1"
    )[0].memory_space_id == "realm-1"
    # Backwards-compat: no default supplied → legacy wing fallback.
    assert parse_search_tool_payload(stripped)[0].memory_space_id == "Wing_Life"
    # Metadata-carried id (get_all / sqlite_exact paths) always wins over the
    # default, so a genuinely cross-space record keeps its real id.
    with_meta = {"results": [{"wing": "Wing_Life", "room": "r1", "text": "x",
                              "metadata": {"memory_space_id": "realm-2"}}]}
    assert parse_search_tool_payload(
        with_meta, default_memory_space_id="realm-1"
    )[0].memory_space_id == "realm-2"


def test_stamped_vector_hit_visible_and_cross_space_rejected():
    """A stripped vector hit stamped with the caller's space is recalled (not
    filtered), while a hit that carries a different real space id is rejected."""
    from eidolon.memory.adapters.search_payload import parse_search_tool_payload

    ctx = _context()  # memory_space_id == "default.alice.default"
    hit = {"results": [{"wing": "Wing_Life", "room": "r1", "text": "alice fact"}]}
    rec = parse_search_tool_payload(hit, default_memory_space_id=ctx.memory_space_id)[0]
    assert recall_record_visible_for_context(rec, ctx)

    other = {"results": [{"wing": "Wing_Life", "room": "r1", "text": "bob fact",
                          "metadata": {"memory_space_id": "default.bob.default"}}]}
    rec_other = parse_search_tool_payload(other, default_memory_space_id=ctx.memory_space_id)[0]
    assert not recall_record_visible_for_context(rec_other, ctx)


def test_json_content_drawer_dedups_across_read_paths():
    """The same JSON-content drawer read via the vector path (parsed dict) and
    the get_all fallback (raw JSON string) must collapse to one record; a
    genuinely different drawer must not."""
    from eidolon.memory.application.public_recall import _merge_unique_records
    from eidolon.memory.domain.wire import MemoryWireRecord

    vec = MemoryWireRecord(  # vector path: value is the PARSED object
        memory_space_id="realm", key="r1", value={"b": 2, "a": 1},
        metadata={"wing": "Wing_Life", "room": "r1"},
    )
    getall = MemoryWireRecord(  # get_all fallback: same drawer, RAW json string, drawer-id key
        memory_space_id="realm", key="drawer-id-xyz", value='{"a": 1, "b": 2}',
        metadata={"wing": "Wing_Life", "room": "r1"},
    )
    assert len(_merge_unique_records([vec], [getall])) == 1

    other = MemoryWireRecord(
        memory_space_id="realm", key="r2", value="different drawer",
        metadata={"wing": "Wing_Life", "room": "r2"},
    )
    assert len(_merge_unique_records([vec], [other])) == 2


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
