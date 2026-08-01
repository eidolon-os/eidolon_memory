"""The shared-database graph, against a server that actually runs it.

This closes the gap test_kg_dialects.py cannot: that one proves the two
implementations build matching statements, this one proves PostgreSQL accepts
them and answers as expected. Both are needed — structural agreement with a
query the server rejects is worth nothing.

No longer gated behind an environment variable. The server comes from
``pgserver`` (see postgres_fixture), so these run in an ordinary suite and the
cloud graph is tested rather than merely reviewed. Point
``EIDOLON_MEMORY_PG_TEST_DSN`` at another server to check a specific version.
"""

from __future__ import annotations

import pytest

OWNER = "owner"
COMPANION_A = "companion:comp_a"
COMPANION_B = "companion:comp_b"


@pytest.fixture
async def graph(postgres_pool):
    """A graph on a real server, in a schema this test owns."""

    from eidolon.memory.adapters.kg_postgres import PostgresKnowledgeGraph

    made = PostgresKnowledgeGraph(postgres_pool, space_id="alice")
    await made.ensure_schema()
    return made


async def test_the_server_accepts_the_shared_schema(graph) -> None:
    """The claim test_kg_dialects.py cannot make on its own."""

    assert await graph.stats() == {
        "entities": 0,
        "triples_total": 0,
        "triples_active": 0,
        "triples_invalidated": 0,
        "mentions": 0,
    }


async def test_a_statement_round_trips(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER
    )

    found = await graph.query_entity("alice", audiences=(OWNER,))

    assert [(r.subject, r.predicate, r.object) for r in found] == [
        ("alice", "lives_in", "berlin")
    ]


async def test_validity_intervals_behave_as_they_do_locally(graph) -> None:
    """String timestamps have to order correctly here too, not just in SQLite."""

    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER,
        valid_from="2020-01-01",
    )
    await graph.invalidate(
        subject="alice", predicate="lives_in", object="berlin", ended="2024-01-01"
    )

    assert await graph.query_entity("alice", audiences=(OWNER,)) == []
    earlier = await graph.query_entity("alice", audiences=(OWNER,), as_of="2022-06-01")
    assert [r.object for r in earlier] == ["berlin"]


async def test_superseding_leaves_no_gap(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER,
        valid_from="2020-01-01",
    )
    await graph.supersede(
        subject="alice", predicate="lives_in", old_object="berlin",
        new_object="lisbon", audience=OWNER, changed_at="2024-01-01",
    )

    at_boundary = await graph.query_entity(
        "alice", audiences=(OWNER,), as_of="2024-01-01"
    )
    assert [r.object for r in at_boundary] == ["lisbon"]


async def test_audience_filtering_works_on_the_server(graph) -> None:
    """The boolean column and the IN clause, exercised where they are typed."""

    await graph.add_triple(
        subject="alice", predicate="called_by", object="frog prince",
        audience=COMPANION_A,
    )

    assert await graph.query_entity("alice", audiences=(OWNER, COMPANION_A))
    assert await graph.query_entity("alice", audiences=(OWNER, COMPANION_B)) == []


async def test_sensitivity_is_a_boolean_here_and_still_filters(graph) -> None:
    """The one place the two schemas differ, so the one worth checking live."""

    await graph.add_triple(
        subject="alice", predicate="has_health_condition", object="asthma",
        audience=OWNER,
    )

    assert await graph.query_entity("alice", audiences=(OWNER,)) == []
    opted_in = await graph.query_entity(
        "alice", audiences=(OWNER,), include_sensitive=True
    )
    assert [r.object for r in opted_in] == ["asthma"]


async def test_the_window_function_bounds_each_subject(graph) -> None:
    """ROW_NUMBER() OVER PARTITION BY, on the server that has to plan it."""

    for index in range(6):
        await graph.add_triple(
            subject="alice", predicate="likes", object=f"thing-{index}",
            audience=OWNER,
        )
    await graph.add_triple(
        subject="bob", predicate="likes", object="tea", audience=OWNER
    )

    found = await graph.query_subjects(
        ["alice", "bob"], audiences=(OWNER,), limit_per_subject=2
    )
    per_subject: dict[str, int] = {}
    for record in found:
        per_subject[record.subject] = per_subject.get(record.subject, 0) + 1

    assert per_subject == {"alice": 2, "bob": 1}


async def test_replaying_a_turn_does_not_duplicate(graph) -> None:
    """The conflict target has to be right, or this raises instead of ignoring."""

    first = await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-1",
    )
    again = await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-1",
    )

    assert first == again
    assert (await graph.stats())["triples_total"] == 1


async def test_two_spaces_in_one_database_cannot_see_each_other(graph) -> None:
    """Here the space column is the only boundary, not a second line of defence."""

    from eidolon.memory.adapters.kg_postgres import PostgresKnowledgeGraph

    other = PostgresKnowledgeGraph(graph._pool, space_id="bob")

    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER
    )

    assert await graph.query_entity("alice", audiences=(OWNER,))
    assert await other.query_entity("alice", audiences=(OWNER,)) == []
    assert (await other.stats())["triples_total"] == 0


async def test_aliases_resolve(graph) -> None:
    from eidolon.memory.adapters.kg_sqlite import entity_id_for

    await graph.add_triple(
        subject="robert", predicate="likes", object="tea", audience=OWNER
    )
    await graph.record_entity_mention(
        entity_id=entity_id_for("robert"), alias="my dad", source="steward"
    )

    assert "robert" in await graph.match_entities_for_query("what does my dad like", cap=3)


async def test_it_holds_no_lock(graph) -> None:
    """Confirmed against a real pool, not just the class attribute."""

    assert graph.lock is None
