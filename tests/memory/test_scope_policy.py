from __future__ import annotations

from types import SimpleNamespace

from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience, council_audience

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.application.scope_policy import (
    derived_triple_audience,
    interaction_audience,
)
from eidolon.memory.domain.space_lock import SpaceLock


def test_interaction_scope_is_narrow_by_default() -> None:
    assert interaction_audience(SimpleNamespace(companion_id="mochi")) == companion_audience(
        "mochi"
    )
    assert interaction_audience(
        SimpleNamespace(companion_id="mochi", council_id="weekly")
    ) == council_audience("weekly")
    assert interaction_audience(SimpleNamespace(companion_id=None)) == OWNER_AUDIENCE


def test_only_stable_low_risk_facts_are_promoted() -> None:
    context = SimpleNamespace(companion_id="mochi", council_id=None)
    assert derived_triple_audience("lives_in", context) == OWNER_AUDIENCE
    assert derived_triple_audience("prefers", context) == OWNER_AUDIENCE
    assert derived_triple_audience("friend_of", context) == companion_audience("mochi")
    assert derived_triple_audience("promised", context) == companion_audience("mochi")
    assert derived_triple_audience("has_health_condition", context) == companion_audience(
        "mochi"
    )


async def test_graph_dedup_and_invalidation_are_audience_scoped(tmp_path) -> None:
    graph = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id="realm", lock=SpaceLock()
    )
    private = companion_audience("mochi")
    try:
        owner_id = await graph.add_triple(
            subject="用户", predicate="likes", object="茶", audience=OWNER_AUDIENCE
        )
        private_id = await graph.add_triple(
            subject="用户", predicate="likes", object="茶", audience=private
        )
        assert owner_id != private_id

        changed = await graph.invalidate(
            subject="用户",
            predicate="likes",
            object="茶",
            audiences=(private,),
        )
        assert changed == 1
        owner_rows = await graph.query_entity(
            "用户", audiences=(OWNER_AUDIENCE,), direction="outgoing"
        )
        private_rows = await graph.query_entity(
            "用户", audiences=(private,), direction="outgoing"
        )
        assert [row.object for row in owner_rows] == ["茶"]
        assert private_rows == []
    finally:
        graph.close()
