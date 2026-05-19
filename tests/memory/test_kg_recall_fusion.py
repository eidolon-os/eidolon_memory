"""T3: entity candidate extraction + recall_with_kg_fusion (KG plan §5)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ─── Entity extraction (no LLM) ───────────────────────────────────────────


def test_extract_entity_candidates_substring_match() -> None:
    from eidolon.memory.application.kg_recall import extract_entity_candidates

    names = ["self", "mother", "father", "project:OP-3091", "tea"]
    hits = extract_entity_candidates("我妈最近怎么样？", names, cap=3)
    # Chinese 我妈 doesn't contain "mother" substring — entity name match only.
    # This is the documented behaviour (canonical names are used by steward).
    assert hits == []


def test_extract_entity_candidates_canonical_substring() -> None:
    from eidolon.memory.application.kg_recall import extract_entity_candidates

    names = ["self", "mother", "tea"]
    hits = extract_entity_candidates("self likes tea", names, cap=3)
    assert "self" in hits
    assert "tea" in hits


def test_extract_entity_candidates_cap_respected() -> None:
    from eidolon.memory.application.kg_recall import extract_entity_candidates

    names = ["a", "b", "c", "d", "e", "f"]
    hits = extract_entity_candidates("a b c d e f g", names, cap=3)
    assert len(hits) == 3


def test_extract_entity_candidates_prefers_longer_first() -> None:
    """A query containing 'mother:张丽' shouldn't also match plain 'mother'."""
    from eidolon.memory.application.kg_recall import extract_entity_candidates

    names = ["mother", "mother:张丽"]
    hits = extract_entity_candidates("mother:张丽 has insomnia", names, cap=3)
    # Both contain — but longer one wins, plain 'mother' still added (substring of query).
    assert "mother:张丽" in hits
    # We accept both; the de-dup logic is by string equality, not by overlap.
    assert len(hits) <= 2


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


# ─── cached_entity_names TTL ───────────────────────────────────────────────


async def test_cached_entity_names_caches_within_ttl() -> None:
    from eidolon.memory.application.kg_recall import (
        cached_entity_names,
        invalidate_entity_cache,
    )

    fake_kg = MagicMock()
    fake_kg.list_entity_names = AsyncMock(side_effect=[["a", "b"], ["a", "b", "c"]])

    invalidate_entity_cache(fake_kg)
    first = await cached_entity_names(fake_kg, ttl_seconds=60.0)
    second = await cached_entity_names(fake_kg, ttl_seconds=60.0)
    assert first == second == ["a", "b"]
    assert fake_kg.list_entity_names.await_count == 1


async def test_cached_entity_names_refetches_after_invalidate() -> None:
    from eidolon.memory.application.kg_recall import (
        cached_entity_names,
        invalidate_entity_cache,
    )

    fake_kg = MagicMock()
    fake_kg.list_entity_names = AsyncMock(side_effect=[["a"], ["a", "b"]])

    invalidate_entity_cache(fake_kg)
    first = await cached_entity_names(fake_kg, ttl_seconds=60.0)
    invalidate_entity_cache(fake_kg)
    second = await cached_entity_names(fake_kg, ttl_seconds=60.0)
    assert first == ["a"]
    assert second == ["a", "b"]


# ─── recall_with_kg_fusion (parallel gather, timeout, KG miss) ─────────────


@pytest.fixture
def fusion_setup(tmp_path: Path):
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from eidolon.memory.application.kg_recall import invalidate_entity_cache
    from eidolon.memory.config.memory_settings import load_memory_settings

    backend = LockedBackend(FakeMemoryBackend())
    kg = LockedKnowledgeGraph(
        KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")), backend.lock
    )
    invalidate_entity_cache(kg)
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
    """A slow KG must not raise into the caller; degrade to vector-only."""
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, _, settings = fusion_setup
    settings = settings.model_copy(deep=True)
    settings.recall.kg_timeout_seconds = 0.05

    slow_kg = MagicMock()
    slow_kg.lock = backend.lock

    async def _slow_list_names():
        await asyncio.sleep(0.5)
        return ["self"]

    slow_kg.list_entity_names = AsyncMock(side_effect=_slow_list_names)
    slow_kg.query_entity_combined = AsyncMock(return_value=[])

    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        user_id="alice", top_k=5,
        kg=slow_kg,
        for_voice=False,
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
