"""T3: entity candidate extraction + recall_with_kg_fusion (KG plan §5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.domain.space_lock import SpaceLock

SPACE_FOR_TESTS = "default.alice.default"

def _ctx(memory_realm_id: str = "default.alice.default") -> MemoryActorContext:
    return MemoryActorContext(
        memory_realm_id=memory_realm_id,
        owner_id="alice",
        companion_id="default",
        device_id="device",
        session_id="unit",
    )


# ─── Entity routing — KG facade owns the naming-convention bridge ────────


async def _seed_entities(kg, names_with_types: list[tuple[str, str]]) -> None:
    """Register entities by asserting a statement about each.

    Entities exist because something was said about them, so seeding goes through
    the write path rather than reaching into storage. The predicate and object are
    incidental — these tests are about how a name is matched, not what is claimed —
    and every entity shares one object so the seed adds a single extra name rather
    than one per row.
    """

    for name, entity_type in names_with_types:
        await kg.add_triple(
            subject=name,
            predicate="holds_role",
            object=f"seed-{entity_type}",
            audience="owner",
        )


async def test_match_entities_bare_name_substring(tmp_path: Path) -> None:
    pytest.importorskip("mempalace")
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=SpaceLock()
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
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=SpaceLock()
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
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=SpaceLock()
    )
    try:
        await _seed_entities(kg, [(c, "unknown") for c in "abcdef"])
        hits = await kg.match_entities_for_query("a b c d e f", cap=3)
        assert len(hits) == 3
    finally:
        kg.close()


async def test_match_entities_prefers_longer(tmp_path: Path) -> None:
    """When both ``mother`` and ``mother:张丽`` would match, prefer the
    longer canonical so prefixed entities beat their bare tails.
    """
    pytest.importorskip("mempalace")
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=SpaceLock()
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
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=SpaceLock()
    )
    try:
        await _seed_entities(kg, [("self", "unknown")])
        assert await kg.match_entities_for_query("", cap=3) == []
        assert await kg.match_entities_for_query("   ", cap=3) == []
    finally:
        kg.close()


# ─── transcription ─────────────────────────────────────────────────────────


def test_transcribe_triple_promised_with_valid_to() -> None:
    from eidolon.memory.application.kg_recall import INFERRED_MARK, transcribe_triple
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
    # To the minute, because the deadline was a real time. A date-only value is
    # widened to midnight on write and renders as the bare day instead.
    assert "截至 2026-05-26 23:59" in out
    assert INFERRED_MARK in out, "a derived fact must not read like something said"


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


def test_transcribe_pet_role_as_breed_identity() -> None:
    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    t = KgTripleRecord(
        id="t1",
        subject="pet:铁锤",
        predicate="holds_role",
        object="边境牧羊犬",
        valid_from=None,
        valid_to=None,
    )
    out = transcribe_triple(t)
    assert "铁锤 的品种/身份是 边境牧羊犬" in out
    assert "pet:铁锤" not in out
    assert "担任" not in out


# ─── the transcription reaches the model, so a bad one is a stated falsehood ──
#
# These lines go into the [MEMORY] block and from there into the prompt. Eight of
# the thirty-two predicates used to render as broken sentences: the table mixed a
# fragment-with-ellipsis shape for relational predicates ("是…的孩子") with a
# bare-verb shape for the rest ("喜欢"), and the renderer concatenated
# subject + entry + object either way. So "铁锤 是…的孩子 用户" reached the model —
# in exactly the kinship and employment relations kinship_alias tests.


def test_every_predicate_template_carries_both_slots() -> None:
    """One shape, asserted, so the mixed-shape bug cannot come back.

    A template missing ``{o}`` silently drops the object; one missing ``{s}``
    drops the subject. Neither raises — the line just states something else.
    """

    from eidolon.memory.domain.predicates import _PREDICATE_TEMPLATES as _PREDICATE_ZH

    for predicate, template in _PREDICATE_ZH.items():
        assert "{s}" in template, f"{predicate} has no subject slot: {template!r}"
        assert "{o}" in template, f"{predicate} has no object slot: {template!r}"
        assert "…" not in template, (
            f"{predicate} still holds an ellipsis placeholder: {template!r}"
        )


def test_relational_predicates_read_as_sentences() -> None:
    """The eight that were broken, checked as output rather than as a table."""

    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    expected = {
        ("pet:铁锤", "child_of", "self"): "铁锤 是 用户 的孩子",
        ("self", "parent_of", "child:小明"): "用户 是 小明 的父母",
        ("self", "partner_of", "partner:王芳"): "用户 是 王芳 的伴侣",
        ("self", "sibling_of", "sister:小红"): "用户 是 小红 的兄弟姐妹",
        ("self", "friend_of", "friend:阿强"): "用户 和 阿强 是朋友",
        ("self", "colleague_of", "boss:李总"): "用户 和 李总 是同事",
        ("self", "works_at", "org:某公司"): "用户 在 某公司 工作",
        ("self", "studies_at", "org:某大学"): "用户 在 某大学 学习",
    }

    for (subject, predicate, object_), sentence in expected.items():
        out = transcribe_triple(
            KgTripleRecord(id="t", subject=subject, predicate=predicate, object=object_)
        )
        assert sentence in out, f"{predicate} rendered as {out!r}"


def test_the_owner_is_not_named_self_in_the_prompt() -> None:
    """``self`` is how the graph stores the owner, not a word for a model to read.

    Untranslated it arrives as "self 计划 去日本" — a schema token presented as
    part of a fact about the user.
    """

    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    out = transcribe_triple(
        KgTripleRecord(id="t", subject="self", predicate="planned_to", object="去日本")
    )

    assert "self" not in out
    assert "用户 计划 去日本" in out


def test_an_unknown_predicate_still_produces_a_line() -> None:
    """Dropping the fact would be worse than rendering it awkwardly."""

    from eidolon.memory.application.kg_recall import transcribe_triple
    from eidolon.memory.domain.kg import KgTripleRecord

    out = transcribe_triple(
        KgTripleRecord(id="t", subject="self", predicate="invented_predicate", object="X")
    )

    assert "用户" in out
    assert "X" in out


# ─── list_entity_names freshness (post-cache-deletion architecture) ───────


async def test_list_entity_names_reflects_write_immediately(tmp_path: Path) -> None:
    """After cache deletion (D1 reflection):每次 recall 直读 entities 表,
    写入后下一次读必须立刻看到新实体——不依赖任何 TTL / invalidate 调用。
    """
    pytest.importorskip("mempalace")
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    lock = SpaceLock()
    kg = SqliteKnowledgeGraph(
        tmp_path / "freshness.sqlite3", space_id=SPACE_FOR_TESTS, lock=lock
    )
    try:
        names0 = await kg.list_entity_names()
        assert "self" not in names0

        await kg.add_triple(
            audience="owner",
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
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
    from eidolon.memory.adapters.locked_backend import LockedBackend
    from eidolon.memory.config.memory_settings import load_memory_settings

    backend = LockedBackend(FakeMemoryBackend())
    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE_FOR_TESTS, lock=backend.lock)
    settings = load_memory_settings()
    yield backend, kg, settings
    kg.close()


async def test_fusion_kg_path_fires_on_entity_hit(fusion_setup) -> None:
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup
    await kg.add_triple(
        audience="owner",
        subject="self", predicate="likes", object="tea", confidence=0.95,
        source_turn_id="seed", adapter_name="test",
    )

    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        context=_ctx(), top_k=5,
        kg=kg,
        for_voice=False,
    )
    assert any(t.predicate == "likes" and t.object == "tea" for t in result["kg"])


async def test_fusion_kg_path_skipped_when_no_entity_match(fusion_setup) -> None:
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup
    await kg.add_triple(
        audience="owner",
        subject="alice", predicate="likes", object="tea", confidence=0.95,
        source_turn_id="seed", adapter_name="test",
    )

    # Query mentions nothing in KG → no fan-out.
    result = await recall_with_kg_fusion(
        backend, settings,
        query="今天天气怎么样",
        context=_ctx(), top_k=5,
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
        audience="owner",
        subject="self", predicate="likes", object="tea", confidence=0.95,
        source_turn_id="seed", adapter_name="test",
    )
    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        context=_ctx(), top_k=5,
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
        context=_ctx(), top_k=5,
        kg=slow_kg,
        for_voice=True,  # voice path uses the strict kg_timeout_seconds
    )
    assert result["kg"] == []
    # Vector path still ran (returned empty for FakeMemoryBackend, but no error)
    assert "vector" in result


async def test_a_caller_hint_adds_to_the_phrase_rather_than_replacing_it(
    fusion_setup,
) -> None:
    """``focus_subjects`` used to discard everything found in the phrase.

    The contract calls it "a hint, not a directive: the service may use it to
    sharpen retrieval". Overriding is not sharpening — a caller naming one entity
    silently lost every other entity the person had just mentioned, which is the
    opposite of what a hint is for.

    Both seeds now reach one query, in both directions, because a caller naming
    an entity wants what is known about it and half of that is incoming.
    """

    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.domain.kg import KgTripleRecord

    backend, _, settings = fusion_setup
    kg = MagicMock()
    kg.match_entities_for_query = AsyncMock(return_value=["from-the-phrase"])
    kg.entities_for_source_turns = AsyncMock(return_value=[])
    kg.query_entity_combined = AsyncMock(
        return_value=[
            KgTripleRecord(id="t1", subject="self", predicate="likes", object="tea")
        ]
    )

    result = await recall_with_kg_fusion(
        backend,
        settings,
        query="an arbitrary personal-memory question",
        context=_ctx(),
        top_k=5,
        kg=kg,
        kg_subjects=["self"],
    )

    assert [row.id for row in result["kg"]] == ["t1"]
    kg.match_entities_for_query.assert_awaited(), "the phrase must still be read"
    seeds = kg.query_entity_combined.await_args_list[0].args[0]
    assert seeds == ["self", "from-the-phrase"], (
        "the hint leads, but it does not evict what the phrase found"
    )
    assert kg.query_entity_combined.await_args_list[0].kwargs == {
        # Both layers: the owner's own facts, plus what this companion was told.
        "audiences": ("owner", "companion:default"),
        "as_of": None,
        "include_sensitive": False,
        "limit_per_entity": settings.recall.kg_max_triples_per_entity,
    }


async def test_group_recall_context_appends_kg_section() -> None:
    from eidolon.memory.application.recall_renderer import group_recall_context
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
    # The heading is gone on purpose — it named the storage in text the model
    # reads, and it made the graph a removable block. Each line carries its own
    # mark instead.
    assert "知识图谱事实" not in out
    assert "（推测）" in out
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


# ─── Phase 1 — rerank wired into recall_with_kg_fusion ─────────────────────


async def _seed_text(backend, wing: str, *, key: str, text: str) -> None:
    """Bypass MemoryFragment validators — go straight to ingest_text."""
    await backend.ingest_text(
        wing=wing, room=key, text=text,
        metadata={"memory_type": "preference"},
    )


async def test_recall_with_rerank_is_invoked_in_pipeline(
    fusion_setup, monkeypatch
) -> None:
    """Phase 1 wire-up: ``recall_with_kg_fusion`` must call ``rerank_bm25_rrf``
    on the vector hits with the configured ``rrf_k`` and ``top_k``.

    Spies the rerank function so we exercise the integration boundary without
    fighting FakeBackend's overly-strict substring filter.
    """
    from eidolon.memory.application import public_recall

    backend, _kg, settings = fusion_setup
    wing = next(w.id for w in settings.wings if w.id != "Wing_Privacy")
    # Seed three docs that all match the substring "用户".
    await _seed_text(backend, wing, key="noise-1", text="用户最近在听 Acquired 播客")
    await _seed_text(backend, wing, key="noise-2", text="用户最近在思考人生")
    await _seed_text(backend, wing, key="tea",     text="用户喝乌龙茶不喝咖啡")

    calls: list[dict] = []
    real_rerank = public_recall.rerank_bm25_rrf

    def _spy(query, hits, *, top_k, rrf_k):
        calls.append({
            "query": query, "n_hits": len(hits),
            "top_k": top_k, "rrf_k": rrf_k,
        })
        return real_rerank(query, hits, top_k=top_k, rrf_k=rrf_k)

    monkeypatch.setattr(public_recall, "rerank_bm25_rrf", _spy)

    result = await public_recall.recall_with_kg_fusion(
        backend, settings,
        query="用户",      # matches all 3 in FakeBackend
        context=_ctx(wing), top_k=3,
        kg=None,
        for_voice=False,
    )
    # Sanity: full pipeline ran and rerank was applied.
    assert len(result["vector"]) == 3
    assert calls and calls[0]["n_hits"] == 3, (
        f"rerank not invoked or wrong arity: {calls}"
    )
    assert calls[0]["top_k"] == 3
    assert calls[0]["rrf_k"] == settings.recall.rerank_rrf_k


async def test_recall_with_rerank_can_be_disabled_via_settings(
    fusion_setup, monkeypatch
) -> None:
    """``rerank_enabled=False`` → bypass rerank entirely (zero-cost rollback).

    Spies ``rerank_bm25_rrf`` and asserts it was *never* called when the
    flag is off, while the recall pipeline still returns vector hits.
    """
    from eidolon.memory.application import public_recall

    backend, _kg, settings = fusion_setup
    settings = settings.model_copy(deep=True)
    settings.recall.rerank_enabled = False

    wing = next(w.id for w in settings.wings if w.id != "Wing_Privacy")
    for i, text in enumerate(["用户 alpha", "用户 beta", "用户 gamma"]):
        await _seed_text(backend, wing, key=f"k{i}", text=text)

    calls: list[int] = []

    def _spy(*args, **kwargs):
        calls.append(1)
        raise AssertionError("rerank should not be called when disabled")

    monkeypatch.setattr(public_recall, "rerank_bm25_rrf", _spy)

    result = await public_recall.recall_with_kg_fusion(
        backend, settings,
        query="用户",      # substring-matches all 3 seeded docs
        context=_ctx(wing), top_k=3,
        kg=None,
        for_voice=False,
    )
    assert calls == [], "rerank ran despite rerank_enabled=False"
    assert len(result["vector"]) == 3


async def test_recall_kg_triples_ordered_by_confidence(fusion_setup) -> None:
    """Phase 1.2: KG SQL `ORDER BY confidence DESC` — cap keeps high-conf facts.

    Insert 3 triples for the same entity with descending confidence; cap=2
    must keep the top two by confidence regardless of insertion order.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup
    settings = settings.model_copy(deep=True)
    settings.recall.kg_max_triples_per_entity = 2

    # Insert lowest-confidence first to prove ORDER BY (not insertion order) wins.
    await kg.add_triple(
        audience="owner",
        subject="self", predicate="likes", object="bitter-tea",
        confidence=0.30, source_turn_id="low", adapter_name="test",
    )
    await kg.add_triple(
        audience="owner",
        subject="self", predicate="likes", object="oolong",
        confidence=0.95, source_turn_id="high", adapter_name="test",
    )
    await kg.add_triple(
        audience="owner",
        subject="self", predicate="likes", object="green-tea",
        confidence=0.70, source_turn_id="mid", adapter_name="test",
    )

    result = await recall_with_kg_fusion(
        backend, settings,
        query="self likes tea",
        context=_ctx(), top_k=5,
        kg=kg,
        for_voice=False,
    )
    objects = {t.object for t in result["kg"]}
    # Cap=2 → must contain the two highest-confidence (oolong 0.95, green-tea 0.70),
    # never the 0.30 bitter-tea.
    assert "oolong" in objects
    assert "green-tea" in objects
    assert "bitter-tea" not in objects, f"low-conf triple leaked past cap: {objects}"


# ─── Phase 3 — alias-driven recall fusion ──────────────────────────────────


async def test_recall_with_alias_query_hits_kg_via_mentions(fusion_setup) -> None:
    """Full local pipeline: seed an alias row → query with the alias →
    ``recall_with_kg_fusion`` returns the triple for the canonical entity.

    Bridges Phase 3c (KG alias lookup) and the existing fusion machinery
    without needing a real LLM — uses ``record_entity_mention`` directly
    to simulate what a successful steward-emitted mention would land.
    """
    from eidolon.memory.application.public_recall import recall_with_kg_fusion

    backend, kg, settings = fusion_setup

    # Seed canonical entity with a triple, then attach the colloquial alias.
    await kg.add_triple(
        audience="owner",
        subject="mother:张丽", predicate="has_state", object="insomnia",
        confidence=0.95, source_turn_id="seed-alias", adapter_name="test",
    )
    await kg.record_entity_mention(
        entity_id="mother:张丽", alias="我妈",
        source="steward-llm", confidence=0.95,
    )

    result = await recall_with_kg_fusion(
        backend, settings,
        query="我妈最近怎样",         # natural-language alias only
        context=_ctx(), top_k=5,
        kg=kg,
        for_voice=False,
    )
    # Canonical entity surfaces via strategy 3 alias lookup, and its triple
    # comes through the fusion pipeline.
    assert any(
        t.subject == "mother:张丽" and t.object == "insomnia"
        for t in result["kg"]
    ), f"alias 'I妈' failed to route to mother:张丽 in fusion: {result['kg']}"


async def test_the_graph_answers_a_question_that_names_nobody(fusion_setup) -> None:
    """The inversion this whole seeding change exists to fix.

    "她住哪儿" contains no entity, so phrase matching finds nothing and the graph
    used to contribute nothing — in precisely the turns where it has the most to
    add, since a question that *does* name someone is one the vector store was
    going to answer anyway.

    Vector search still finds the right memory. That memory's turn produced
    statements, and one hop out from their entities is what the graph is for.
    """

    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.domain.kg import KgTripleRecord

    from eidolon.memory.domain.wire import MemoryWireRecord

    backend, _, settings = fusion_setup
    # The memory vector search finds. Its turn is the bridge into the graph.
    backend._inner.docs[f"{SPACE_FOR_TESTS}::drawer_1"] = MemoryWireRecord(
        memory_space_id=SPACE_FOR_TESTS,
        key="drawer_1",
        # The fake backend matches on containment, so the drawer carries the
        # phrase. What is being tested is the seeding, not vector search.
        value="上周妈妈提过她住哪儿这件事",
        metadata={
            "memory_space_id": SPACE_FOR_TESTS,
            "wing": "Wing_Profile",
            "source_turn_id": "the-turn-about-mother",
        },
    )

    kg = MagicMock()
    kg.match_entities_for_query = AsyncMock(return_value=[])  # the phrase names nobody
    kg.entities_for_source_turns = AsyncMock(return_value=["mother:张丽"])
    kg.query_entity_combined = AsyncMock(
        return_value=[
            KgTripleRecord(
                id="t-hop",
                subject="mother:张丽",
                predicate="lives_in",
                object="杭州",
                source_turn_id="some-other-turn",
            )
        ]
    )

    result = await recall_with_kg_fusion(
        backend, settings, query="她住哪儿", context=_ctx(), top_k=5, kg=kg
    )

    assert [row.id for row in result["kg"]] == ["t-hop"]
    kg.entities_for_source_turns.assert_awaited()


async def test_a_restating_triple_loses_the_budget_but_is_not_deleted(
    fusion_setup,
) -> None:
    """Near-redundant, so it sorts last; still present, so a young graph is not silent.

    A statement from a turn already showing as a drawer mostly repeats what the
    drawer says in the person's own words, and should lose its place to a fact
    the drawers did not carry. Dropping it outright was the first attempt and it
    was wrong: on a young palace almost every statement shares a turn with a
    recalled drawer, so the graph would go quiet exactly where it is being asked
    to help — the same failure, wearing a different hat.
    """

    from eidolon.memory.application.public_recall import _merge_triples
    from eidolon.memory.domain.kg import KgTripleRecord

    def triple(identity: str, turn: str) -> KgTripleRecord:
        return KgTripleRecord(
            id=identity, subject="s", predicate="likes", object="o", source_turn_id=turn
        )

    merged = _merge_triples(
        [triple("restates", "shown-turn"), triple("novel", "other-turn")],
        [triple("restates", "shown-turn")],  # the same statement from both seeds
        already_shown={"shown-turn"},
        limit=10,
    )

    assert [row.id for row in merged] == ["novel", "restates"]

    # And when the budget binds, the novel one is what survives.
    assert [row.id for row in _merge_triples(
        [triple("restates", "shown-turn"), triple("novel", "other-turn")],
        [],
        already_shown={"shown-turn"},
        limit=1,
    )] == ["novel"]
