"""Tests for shared MCP-style recall (``public_recall``)."""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import MemoryActorContext, build_memory_actor_context

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import (
    recall_record_visible_for_context,
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
)
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.application.scope_policy import MissingInteractionIdentity
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.domain.errors import MemoryBackendUnavailable

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
async def test_ordinary_recall_without_companion_or_council_fails_closed():
    context = MemoryActorContext(
        memory_realm_id=MEMORY_SPACE_ID,
        memory_space_id=MEMORY_SPACE_ID,
        owner_id="alice",
    )

    with pytest.raises(MissingInteractionIdentity):
        await search_all_wings_mcp_style(
            FakeMemoryBackend(),
            get_memory_settings(),
            query="tea",
            context=context,
            top_k=5,
            wing=None,
            room=None,
        )


@pytest.mark.asyncio
async def test_vector_failure_is_reported_without_a_private_fallback():
    class BrokenVectorBackend(FakeMemoryBackend):
        async def search(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("vector index unavailable")

    with pytest.raises(MemoryBackendUnavailable, match="vector search degraded"):
        await search_all_wings_mcp_style(
            BrokenVectorBackend(),
            get_memory_settings(),
            query="我叫什么名字",
            context=_context(),
            top_k=5,
            wing="Wing_Profile",
            room=None,
            raise_on_degraded=True,
        )


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


def test_storage_result_preserves_authoritative_space_and_rejects_other_space():
    from eidolon.memory.adapters.mempalace_results import storage_record

    ctx = _context()
    rec = storage_record(
        "drawer_a",
        "alice fact",
        {"wing": "Wing_Life", "room": "r1"},
        memory_space_id=ctx.memory_space_id,
    )
    assert rec.memory_space_id == ctx.memory_space_id
    assert recall_record_visible_for_context(rec, ctx)
    other = storage_record(
        "drawer_b",
        "bob fact",
        {"memory_space_id": "default.bob.default", "wing": "Wing_Life"},
        memory_space_id=ctx.memory_space_id,
    )
    assert other.memory_space_id == "default.bob.default"
    assert not recall_record_visible_for_context(other, ctx)


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
async def test_voice_and_text_recall_have_the_same_memory_visibility_across_sessions():
    """Transport mode and interaction age affect budgets, never fact visibility."""
    settings = get_memory_settings()
    backend = FakeMemoryBackend()
    recallable_wings = [
        wing.id for wing in settings.wings if wing.id not in {"Wing_Privacy", "Wing_Theme"}
    ]
    for index, wing in enumerate(recallable_wings):
        await backend.ingest_text(
            wing=wing,
            room=f"fact_{index}",
            text=f"青蓝9号跨模态事实 {wing}",
            metadata={
                "memory_space_id": MEMORY_SPACE_ID,
                "audience": "companion:default",
                "scope": "persona",
                "visibility": "all_devices",
                "session_id": "interaction-a",
                "source_turn_id": f"turn-a-{index}",
            },
        )

    same_interaction = _context().model_copy(update={"session_id": "interaction-a"})
    next_interaction = _context().model_copy(update={"session_id": "interaction-b"})
    cases = [
        (same_interaction, False),
        (same_interaction, True),
        (next_interaction, False),
        (next_interaction, True),
    ]
    for context, voice in cases:
        recalled = await recall_with_kg_fusion(
            backend,
            settings,
            query="青蓝9号",
            context=context,
            top_k=len(recallable_wings) + 1,
            kg=None,
            for_voice=voice,
        )
        assert {record.key for record in recalled["vector"]} == {
            f"fact_{index}" for index in range(len(recallable_wings))
        }


@pytest.mark.asyncio
async def test_single_wing_voice_uses_scoped_read_port():
    """Voice recall requests the optimized read capability through its port."""
    settings = get_memory_settings()
    calls: list[list[str]] = []

    class ScopedFake(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, query: str, *, wings: list[str], **kwargs):
            assert query == "test"
            assert kwargs["skip_closets"] is True
            calls.append(list(wings))
            return []

    backend = ScopedFake()
    await search_all_wings_mcp_style(
        backend,
        settings,
        query="test",
        context=_context(),
        top_k=3,
        wing="Wing_Profile",
        room=None,
        for_voice=True,
    )
    assert calls == [["Wing_Profile"]]


@pytest.mark.asyncio
async def test_normal_recall_can_opt_into_scoped_read_without_skipping_closets():
    settings = get_memory_settings().model_copy(deep=True)
    settings.runtime.read.normal_shared_query_embedding = True
    calls: list[list[str]] = []

    class ScopedFake(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, query: str, *, wings: list[str], **kwargs):
            assert query == "test"
            assert kwargs["skip_closets"] is False
            calls.append(list(wings))
            return []

    backend = ScopedFake()
    await search_all_wings_mcp_style(
        backend,
        settings,
        query="test",
        context=_context(),
        top_k=3,
        wing="Wing_Profile",
        room=None,
        for_voice=False,
    )
    assert calls == [["Wing_Profile"]]


@pytest.mark.asyncio
async def test_voice_scoped_read_failure_degrades_to_empty():
    """Chroma/mempalace pyo3 panics can arrive as BaseException wrappers.
    Voice recall must degrade instead of crashing the MCP worker process.
    """
    settings = get_memory_settings()

    class PanicLike(BaseException):
        pass

    class PanickingScopedFake(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, *_args, **_kwargs):
            raise PanicLike("sqlite disk I/O panic")

    backend = PanickingScopedFake()

    out = await search_all_wings_mcp_style(
        backend,
        settings,
        query="test",
        context=_context(),
        top_k=3,
        wing="Wing_Profile",
        room=None,
        for_voice=True,
    )
    assert out == []


@pytest.mark.asyncio
async def test_recall_fusion_marks_degraded_when_voice_scoped_read_fails():
    settings = get_memory_settings()

    class PanicLike(BaseException):
        pass

    class PanickingScopedFake(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, *_args, **_kwargs):
            raise PanicLike("sqlite disk I/O panic")

    backend = PanickingScopedFake()

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


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting_on", ["vector", "theme", "graph"])
async def test_cancel_recall_drains_its_in_flight_channels(waiting_on):
    import asyncio

    started = {name: asyncio.Event() for name in ("vector", "theme", "graph")}
    finished = set()
    release = asyncio.Event()

    async def read(name):
        started[name].set()
        try:
            if name == waiting_on or (name == "graph" and waiting_on == "vector"):
                await release.wait()
            return []
        finally:
            finished.add(name)

    class PendingBackend(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, *args, **kwargs):
            return await read("vector")

        async def search(self, *args, **kwargs):
            return await read("theme")

    class PendingGraph:
        async def match_entities_for_query(self, *args, **kwargs):
            return await read("graph")

    settings = get_memory_settings().model_copy(deep=True)
    settings.recall.kg_timeout_seconds_normal = 10
    task = asyncio.create_task(recall_with_kg_fusion(
        PendingBackend(), settings, query="tea", context=_context(), top_k=1,
        kg=PendingGraph(),
    ))
    try:
        pending = ["vector", "graph"] if waiting_on != "theme" else ["theme"]
        await asyncio.wait_for(
            asyncio.gather(*(started[name].wait() for name in pending)), timeout=1,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == {name for name, event in started.items() if event.is_set()}
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["vector", "theme", "graph"])
async def test_recall_preserves_channel_failure_isolation(failed):
    from eidolon.memory.domain.wire import MemoryWireRecord

    fact = MemoryWireRecord(memory_space_id=MEMORY_SPACE_ID, key="fact", value="tea")
    theme = MemoryWireRecord(memory_space_id=MEMORY_SPACE_ID, key="theme", value="tea habits")

    class FailingBackend(FakeMemoryBackend):
        supports_scoped_search = True

        async def search_scoped(self, *args, **kwargs):
            if failed == "vector":
                raise RuntimeError("vector unavailable")
            return [fact]

        async def search(self, *args, **kwargs):
            if failed == "theme":
                raise RuntimeError("theme unavailable")
            return [theme]

    class FailingGraph:
        async def match_entities_for_query(self, *args, **kwargs):
            if failed == "graph":
                raise RuntimeError("graph unavailable")
            return []

    result = await recall_with_kg_fusion(
        FailingBackend(), get_memory_settings(), query="tea", context=_context(),
        top_k=1, kg=FailingGraph(),
    )
    expected = {"fact", "theme"} - ({"fact"} if failed == "vector" else
                                   {"theme"} if failed == "theme" else set())
    assert {r.key for r in result["vector"]} == expected
    assert result["degraded"] is (failed == "vector")
    assert result["kg"] == []
