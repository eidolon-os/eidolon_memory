"""LockedKnowledgeGraph idempotency + sensitive-predicate filtering (T1)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def kg_pair(tmp_path: Path):
    """Fresh KG file + LockedKnowledgeGraph wrapper sharing one asyncio.Lock."""
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    db = tmp_path / "knowledge_graph.sqlite3"
    inner = KnowledgeGraph(db_path=str(db))
    lock = asyncio.Lock()
    locked = LockedKnowledgeGraph(inner, lock)
    yield locked
    locked.close()


async def test_add_triple_basic_round_trip(kg_pair) -> None:
    triple_id = await kg_pair.add_triple(
        subject="self",
        predicate="likes",
        object="coffee",
        source_turn_id="turn1",
        adapter_name="test",
    )
    assert triple_id

    records = await kg_pair.query_entity("self")
    assert any(r.predicate == "likes" and r.object == "coffee" for r in records)


async def test_add_triple_normalizes_python_utc_datetime(kg_pair) -> None:
    await kg_pair.add_triple(
        subject="self",
        predicate="works_at",
        object="changzhou",
        valid_from="2026-06-28T11:41:17.964620+00:00",
        source_turn_id="turn-time",
        adapter_name="test",
    )

    records = await kg_pair.query_entity("self")
    triple = next(r for r in records if r.predicate == "works_at")
    assert triple.valid_from == "2026-06-28T11:41:17Z"


async def test_add_triple_idempotent_same_turn(kg_pair) -> None:
    """G1: replay of the same source_turn_id must not create duplicates."""
    t1 = await kg_pair.add_triple(
        subject="self", predicate="likes", object="coffee",
        source_turn_id="turn1", adapter_name="test",
    )
    t2 = await kg_pair.add_triple(
        subject="self", predicate="likes", object="coffee",
        source_turn_id="turn1", adapter_name="test",
    )
    assert t1 == t2
    stats = await kg_pair.stats()
    # one triple, two entities (self, coffee)
    assert stats["triples_total"] == 1
    assert stats["entities"] == 2


async def test_add_triple_idempotent_after_invalidate_replay(kg_pair) -> None:
    """The 'add → invalidate → replay original' edge case.

    Without source_turn_id dedup mempalace would add a second 'still valid'
    triple here because the active duplicate check sees nothing.
    """
    t1 = await kg_pair.add_triple(
        subject="self", predicate="likes", object="coffee",
        source_turn_id="turn1", adapter_name="test",
    )
    await kg_pair.invalidate(
        subject="self", predicate="likes", object="coffee", ended="2026-05-01T00:00:00Z"
    )
    t2 = await kg_pair.add_triple(
        subject="self", predicate="likes", object="coffee",
        source_turn_id="turn1", adapter_name="test",
    )
    assert t1 == t2
    stats = await kg_pair.stats()
    assert stats["triples_total"] == 1
    assert stats["triples_active"] == 0  # original was invalidated


async def test_invalidate_idempotent_no_match(kg_pair) -> None:
    rows = await kg_pair.invalidate(
        subject="self", predicate="likes", object="never_existed"
    )
    assert rows == 0


async def test_invalidate_sets_valid_to(kg_pair) -> None:
    await kg_pair.add_triple(
        subject="self", predicate="likes", object="coffee",
        source_turn_id="turn1", adapter_name="test",
    )
    rows = await kg_pair.invalidate(
        subject="self", predicate="likes", object="coffee",
        ended="2026-05-19T00:00:00Z",
    )
    assert rows == 1
    applied = await kg_pair.find_invalidation_applied(
        "self", "likes", "coffee", "2026-05-19T00:00:00Z"
    )
    assert applied


async def test_query_entity_temporal_as_of_filters(kg_pair) -> None:
    await kg_pair.add_triple(
        subject="self", predicate="likes", object="coffee",
        valid_from="2024-01-01T00:00:00Z",
        source_turn_id="turn-a", adapter_name="test",
    )
    await kg_pair.invalidate(
        subject="self", predicate="likes", object="coffee",
        ended="2026-01-01T00:00:00Z",
    )
    # Future as_of: should not appear (invalidated)
    future = await kg_pair.query_entity("self", as_of="2026-06-01T00:00:00Z")
    assert not any(r.object == "coffee" for r in future)
    # Past as_of (between valid_from and valid_to): should appear
    past = await kg_pair.query_entity("self", as_of="2025-01-01T00:00:00Z")
    assert any(r.object == "coffee" for r in past)


async def test_query_entity_filters_sensitive_predicates_by_default(kg_pair) -> None:
    """G2: read tools default to excluding has_health_condition etc."""
    await kg_pair.add_triple(
        subject="self", predicate="has_health_condition", object="hypertension",
        source_turn_id="turn-h", adapter_name="test",
    )
    default = await kg_pair.query_entity("self")
    assert not any(r.predicate == "has_health_condition" for r in default)

    opt_in = await kg_pair.query_entity("self", include_sensitive=True)
    assert any(r.predicate == "has_health_condition" for r in opt_in)


async def test_query_entity_combined_uses_in_clause(kg_pair) -> None:
    """T3 read path: subject IN (...) with cap per entity."""
    await kg_pair.add_triple(
        subject="alice", predicate="friend_of", object="bob",
        source_turn_id="t1", adapter_name="test",
    )
    await kg_pair.add_triple(
        subject="alice", predicate="works_at", object="acme",
        source_turn_id="t2", adapter_name="test",
    )
    await kg_pair.add_triple(
        subject="charlie", predicate="lives_in", object="beijing",
        source_turn_id="t3", adapter_name="test",
    )
    rows = await kg_pair.query_entity_combined(["alice", "charlie"])
    names = {(r.subject, r.predicate) for r in rows}
    assert ("alice", "friend_of") in names
    assert ("alice", "works_at") in names
    assert ("charlie", "lives_in") in names


async def test_list_entity_names_returns_unique_names(kg_pair) -> None:
    await kg_pair.add_triple(
        subject="alice", predicate="friend_of", object="bob",
        source_turn_id="t1", adapter_name="test",
    )
    await kg_pair.add_triple(
        subject="alice", predicate="likes", object="coffee",
        source_turn_id="t2", adapter_name="test",
    )
    names = await kg_pair.list_entity_names()
    assert set(names) == {"alice", "bob", "coffee"}


async def test_lock_serializes_writes(kg_pair) -> None:
    """Two concurrent add_triple via gather must succeed without DB lock errors."""
    await asyncio.gather(
        kg_pair.add_triple(
            subject="self", predicate="likes", object="tea",
            source_turn_id="a", adapter_name="test",
        ),
        kg_pair.add_triple(
            subject="self", predicate="likes", object="wine",
            source_turn_id="b", adapter_name="test",
        ),
        kg_pair.add_triple(
            subject="self", predicate="likes", object="music",
            source_turn_id="c", adapter_name="test",
        ),
    )
    stats = await kg_pair.stats()
    assert stats["triples_total"] == 3


async def test_find_pending_triple_id_returns_existing(kg_pair) -> None:
    tid = await kg_pair.add_triple(
        subject="self", predicate="promised", object="visit_mom",
        source_turn_id="req:abc",
        adapter_name="admin",
    )
    found = await kg_pair.find_pending_triple_id(
        "req:abc", "self", "promised", "visit_mom"
    )
    assert found == tid

    missing = await kg_pair.find_pending_triple_id(
        "req:xyz", "self", "promised", "visit_mom"
    )
    assert missing is None


async def test_timeline_orders_descending(kg_pair) -> None:
    await kg_pair.add_triple(
        subject="self", predicate="attended", object="event_A",
        valid_from="2025-01-01T00:00:00Z",
        source_turn_id="t1", adapter_name="test",
    )
    await kg_pair.add_triple(
        subject="self", predicate="attended", object="event_B",
        valid_from="2026-01-01T00:00:00Z",
        source_turn_id="t2", adapter_name="test",
    )
    rows = await kg_pair.timeline("self", limit=10)
    assert [r.object for r in rows][:2] == ["event_B", "event_A"]
