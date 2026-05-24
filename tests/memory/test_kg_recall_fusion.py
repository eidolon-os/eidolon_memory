"""T3: entity candidate extraction + recall_with_kg_fusion (KG plan §5)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ─── Entity routing — KG facade owns the naming-convention bridge ────────


async def _seed_entities(kg, names_with_types: list[tuple[str, str]]) -> None:
    """Helper: insert raw entities (no triples needed) into the KG fixture."""
    import asyncio

    def _insert() -> None:
        conn = kg._inner._conn()
        for name, etype in names_with_types:
            conn.execute(
                "INSERT OR IGNORE INTO entities(id, name, type, properties, created_at) "
                "VALUES (?, ?, ?, '{}', strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (name, name, etype),
            )
        conn.commit()
    async with kg._lock:
        await asyncio.to_thread(_insert)


async def test_match_entities_bare_name_substring(tmp_path: Path) -> None:
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), asyncio.Lock()
    )
    try:
        await _seed_entities(kg, [("self", "unknown"), ("mother", "unknown"), ("tea", "unknown")])
        hits = await kg.match_entities_for_query("self likes tea", cap=3)
        assert set(hits) == {"self", "tea"}
    finally:
        kg.close()


async def test_match_entities_prefix_stripped(tmp_path: Path) -> None:
    """ROOT-CAUSE regression for 铁锤 bug: query "铁锤是什么" must match
    canonical entity ``pet:铁锤`` even though the type prefix isn't in the
    query string. Steward writes prefixed names for disambiguation; users
    speak in bare names. KG facade bridges the gap.
    """
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), asyncio.Lock()
    )
    try:
        await _seed_entities(kg, [
            ("self", "unknown"),
            ("pet:铁锤", "pet"),
            ("place:北京", "place"),
            ("mother:张丽", "person"),
        ])
        # Each natural-language query reaches its prefixed canonical entity.
        assert "pet:铁锤" in await kg.match_entities_for_query("铁锤是什么", cap=3)
        assert "pet:铁锤" in await kg.match_entities_for_query("铁锤多大了", cap=3)
        assert "place:北京" in await kg.match_entities_for_query("我住北京", cap=3)
        # Literal containment still works (legacy callers).
        assert "pet:铁锤" in await kg.match_entities_for_query("pet:铁锤 多大", cap=3)
    finally:
        kg.close()


async def test_match_entities_cap_respected(tmp_path: Path) -> None:
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), asyncio.Lock()
    )
    try:
        await _seed_entities(kg, [(c, "unknown") for c in "abcdef"])
        hits = await kg.match_entities_for_query("a b c d e f g", cap=3)
        assert len(hits) == 3
    finally:
        kg.close()


async def test_match_entities_prefers_longer(tmp_path: Path) -> None:
    """When both ``mother`` and ``mother:张丽`` would match, prefer the
    longer canonical so prefixed entities beat their bare tails.
    """
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), asyncio.Lock()
    )
    try:
        await _seed_entities(kg, [("mother", "unknown"), ("mother:张丽", "person")])
        hits = await kg.match_entities_for_query("mother:张丽 has insomnia", cap=3)
        assert "mother:张丽" in hits
        # Plain 'mother' is also a literal substring → also matches; both OK.
        assert set(hits) <= {"mother:张丽", "mother"}
    finally:
        kg.close()


async def test_match_entities_empty_query(tmp_path: Path) -> None:
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), asyncio.Lock()
    )
    try:
        await _seed_entities(kg, [("self", "unknown")])
        assert await kg.match_entities_for_query("", cap=3) == []
        assert await kg.match_entities_for_query("   ", cap=3) == []
    finally:
        kg.close()


# ─── transcription ─────────────────────────────────────────────────────────


def test_transcribe_triple_promised_with_valid_to() -> None:
    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    t = KgTripleRecord(
        id="t1",
        subject="self",
        predicate="promised",
        object="visit mother",
        valid_from="2026-05-19T10:00:00Z",
        valid_to="2026-05-26T23:59:59Z",
    )
    out = transcribe_triple(t)
    assert "承诺" in out
    assert "截至 2026-05-26T23:59:59Z" in out


def test_transcribe_triple_invalidated() -> None:
    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    t = KgTripleRecord(
        id="t1",
        subject="self",
        predicate="likes",
        object="coffee",
        valid_from="2024-01-01T00:00:00Z",
        valid_to="2026-01-01T00:00:00Z",
    )
    out = transcribe_triple(t)
    assert "喜欢" in out
    assert "已结束" in out


def test_transcribe_triple_current_state() -> None:
    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    t = KgTripleRecord(
        id="t1",
        subject="mother",
        predicate="has_state",
        object="insomnia",
        valid_from="2026-04-01T00:00:00Z",
        valid_to=None,
    )
    out = transcribe_triple(t)
    assert "处于状态" in out
    assert "自 2026-04-01" in out


# ─── list_entity_names freshness (post-cache-deletion architecture) ───────


async def test_list_entity_names_reflects_write_immediately(tmp_path: Path) -> None:
    """After cache deletion (D1 reflection):每次 recall 直读 entities 表,
    写入后下一次读必须立刻看到新实体——不依赖任何 TTL / invalidate 调用。
    """
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    inner = KnowledgeGraph(db_path=str(tmp_path / "freshness.sqlite3"))
    lock = asyncio.Lock()
    kg = LockedKnowledgeGraph(inner, lock)
    try:
        names0 = await kg.list_entity_names()
        assert "self" not in names0

        await kg.add_triple(
            subject="self", predicate="likes", object="tea",
            confidence=0.95, source_turn_id="freshness", adapter_name="test",
        )
        # Immediately after write — NO sleep, NO invalidate — must see "self".
        names1 = await kg.list_entity_names()
        assert "self" in names1
    finally:
        kg.close()


# ─── recall_with_kg_fusion (parallel gather, timeout, KG miss) ─────────────


@pytest.fixture
def fusion_setup(tmp_path: Path):
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from eidolon.memory.config.memory_settings import load_memory_settings

    backend = LockedBackend(FakeMemoryBackend())
    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), backend.lock
    )
    settings = load_memory_settings()
    yield backend, kg, settings
    kg.close()


async def test_fusion_kg_path_fires_on_entity_hit(fusion_setup) -> None:
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup
    await kg.add_triple(
        subject="self", predicate="likes", object="tea", confidence=0.95,
        source_turn_id="seed", adapter_name="test",
    )

    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        user_id="alice", top_k=5,
        kg=kg,
        for_voice=False,
    )
    assert any(t.predicate == "likes" and t.object == "tea" for t in result["kg"])


async def test_fusion_kg_path_skipped_when_no_entity_match(fusion_setup) -> None:
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup
    await kg.add_triple(
        subject="alice", predicate="likes", object="tea", confidence=0.95,
        source_turn_id="seed", adapter_name="test",
    )

    # Query mentions nothing in KG → no fan-out.
    result = await recall_with_kg_fusion(
        backend, settings,
        query="今天天气怎么样",
        user_id="alice", top_k=5,
        kg=kg,
        for_voice=False,
    )
    assert result["kg"] == []


async def test_fusion_kg_disabled_via_settings(fusion_setup) -> None:
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup
    settings = settings.model_copy(deep=True)
    settings.recall.kg_in_recall = False

    await kg.add_triple(
        subject="self", predicate="likes", object="tea", confidence=0.95,
        source_turn_id="seed", adapter_name="test",
    )
    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        user_id="alice", top_k=5,
        kg=kg,
        for_voice=False,
    )
    assert result["kg"] == []


async def test_fusion_kg_timeout_degrades_silently(fusion_setup) -> None:
    """A slow KG must not raise into the caller; degrade to vector-only.

    Mocks ``match_entities_for_query`` slow so the voice path hits the
    50ms ``kg_timeout_seconds`` budget; recall returns empty kg side
    without raising into the LiveKit caller.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, _, settings = fusion_setup
    settings = settings.model_copy(deep=True)
    settings.recall.kg_timeout_seconds = 0.05

    slow_kg = MagicMock()
    slow_kg.lock = backend.lock

    async def _slow_match(query, *, cap):
        await asyncio.sleep(0.5)
        return ["self"]

    slow_kg.match_entities_for_query = AsyncMock(side_effect=_slow_match)
    slow_kg.query_entity_combined = AsyncMock(return_value=[])

    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        user_id="alice", top_k=5,
        kg=slow_kg,
        for_voice=True,  # voice path uses the strict kg_timeout_seconds
    )
    assert result["kg"] == []
    # Vector path still ran (returned empty for FakeMemoryBackend, but no error)
    assert "vector" in result


async def test_group_recall_context_appends_kg_section() -> None:
    from eidolon.memory.application.public_recall import group_recall_context
    from eidolon.memory.domain.kg import KgTripleRecord

    triple = KgTripleRecord(
        id="t1",
        subject="self",
        predicate="likes",
        object="tea",
        valid_from="2026-05-19T10:00:00Z",
        valid_to=None,
    )
    out = group_recall_context([], kg_triples=[triple])
    assert "知识图谱事实" in out
    assert "喜欢" in out
    assert "tea" in out


async def test_livekit_recall_holds_kg_reference(tmp_path: Path) -> None:
    """LiveKitRecallService accepts and stores kg; deep voice-path fusion
    requires real chromadb and is covered by the end-to-end smoke later."""
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.application.livekit_recall import LiveKitRecallService
    from eidolon.memory.config.memory_settings import load_memory_settings

    backend = LockedBackend(FakeMemoryBackend())
    settings = load_memory_settings()
    svc = LiveKitRecallService(
        backend, settings, palace_path=str(tmp_path), kg="kg-placeholder"
    )
    assert svc._kg == "kg-placeholder"
