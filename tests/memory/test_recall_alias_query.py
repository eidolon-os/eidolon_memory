"""Stored aliases may enrich a query only within the current graph read boundary."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.application.public_recall import _query_with_known_alias
from eidolon.memory.domain.space_lock import SpaceLock


@pytest.fixture
def graph(tmp_path):
    kg = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id="test", lock=SpaceLock())
    yield kg
    kg.close()


async def resolve(graph, query, audiences=("owner",)):
    return await _query_with_known_alias(
        graph,
        query=query,
        audiences=audiences,
        timeout_s=1,
        include_sensitive=False,
    )


async def add(graph, name, alias, *, audience="owner", confidence=0.95, turn="t1"):
    await graph.add_triple(
        subject=name,
        predicate="likes",
        object="阅读",
        audience=audience,
        source_turn_id=turn,
    )
    await graph.record_entity_mention(
        entity_id=name,
        alias=alias,
        audience=audience,
        confidence=confidence,
        source="steward",
    )


async def test_explicit_alias_and_original_question_are_both_preserved(graph):
    await add(graph, "person:林岚", "我的导师")
    assert await resolve(graph, "我的导师喜欢什么") == "我的导师喜欢什么 person:林岚"
    assert await resolve(graph, "林岚喜欢什么") == "林岚喜欢什么"


async def test_weak_reference_and_ambiguous_alias_are_not_rewritten(graph):
    await add(graph, "person:林岚", "她", confidence=0.7)
    assert await resolve(graph, "她喜欢什么") == "她喜欢什么"
    await add(graph, "person:林岚", "导师")
    await add(graph, "person:林岚", "我的导师")
    await add(graph, "person:周川", "导师", turn="t2")
    assert await resolve(graph, "我的导师喜欢什么") == "我的导师喜欢什么"


async def test_alias_cannot_cross_audiences_or_revive_deleted_entity(graph):
    await add(graph, "person:林岚", "导师", audience="companion:a")
    assert await resolve(graph, "导师喜欢什么", ("companion:b",)) == "导师喜欢什么"
    await graph.forget_source_turns(["t1"], hard=True)
    assert await resolve(graph, "导师喜欢什么", ("companion:a",)) == "导师喜欢什么"


async def test_expired_facts_do_not_authorize_alias_enrichment(graph):
    await add(graph, "person:林岚", "导师")
    await graph.forget_source_turns(["t1"])
    assert await resolve(graph, "导师喜欢什么") == "导师喜欢什么"


async def test_timeout_and_failure_fall_back_but_cancellation_propagates():
    graph = AsyncMock()
    graph.match_entities_for_query.side_effect = RuntimeError("unavailable")
    assert await resolve(graph, "导师喜欢什么") == "导师喜欢什么"

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    graph.match_entities_for_query.side_effect = slow
    assert (
        await _query_with_known_alias(
            graph,
            query="导师喜欢什么",
            audiences=("owner",),
            timeout_s=0.001,
            include_sensitive=False,
        )
        == "导师喜欢什么"
    )
    graph.match_entities_for_query.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await resolve(graph, "导师喜欢什么")


async def test_sensitive_only_entity_cannot_enrich_public_query(graph):
    await graph.add_triple(
        subject="person:林岚",
        predicate="likes",
        object="阅读",
        audience="owner",
        sensitive=True,
    )
    await graph.record_entity_mention(entity_id="person:林岚", alias="导师", source="steward")
    assert await resolve(graph, "导师喜欢什么") == "导师喜欢什么"


@pytest.mark.parametrize("voice", [False, True])
async def test_public_recall_uses_resolved_query_without_adding_a_search(graph, monkeypatch, voice):
    from eidolon_memory_contracts import build_memory_actor_context

    from eidolon.memory.application import public_recall
    from eidolon.memory.config.memory_settings import MemorySettings

    await add(graph, "person:林岚", "导师")
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(public_recall, "search_all_wings_mcp_style", search)
    settings = MemorySettings()
    settings.recall.theme_top_k = 0
    await public_recall.recall_with_kg_fusion(
        object(),
        settings,
        query="导师喜欢什么",
        top_k=5,
        kg=graph,
        for_voice=voice,
        context=build_memory_actor_context(
            owner_id="a",
            companion_id="b",
            memory_realm_id="test",
            device_id="d",
            session_id="s",
        ),
    )
    search.assert_awaited_once()
    assert search.await_args.kwargs["query"] == "导师喜欢什么 person:林岚"
    assert search.await_args.kwargs["top_k"] == 5


async def test_readable_alias_does_not_authorize_an_unreadable_fact(graph):
    await graph.add_triple(
        subject="person:林岚",
        predicate="likes",
        object="阅读",
        audience="companion:b",
    )
    await graph.record_entity_mention(
        entity_id="person:林岚",
        alias="导师",
        source="steward",
        audience="owner",
    )
    assert await resolve(graph, "导师喜欢什么") == "导师喜欢什么"
