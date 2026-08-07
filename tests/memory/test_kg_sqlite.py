"""The graph we own: validity intervals, audience, and idempotent writes.

These are the properties the recall path and the turn path depend on. They were
previously spread between MemPalace's implementation and a wrapper around it, so
pinning them here is also what makes replacing the storage safe.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from eidolon.memory.adapters.kg_sqlite import (
    SqliteKnowledgeGraph,
    canonical_temporal,
    entity_id_for,
)
from eidolon.memory.domain.kg_port import KnowledgeGraphPort
from eidolon.memory.domain.space_lock import SpaceLock

OWNER = "owner"
COMPANION_A = "companion:comp_a"
COMPANION_B = "companion:comp_b"
BOTH_FOR_A = (OWNER, COMPANION_A)


@pytest.fixture
def graph(tmp_path):
    made = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id="alice", lock=SpaceLock()
    )
    yield made
    made.close()


def test_it_satisfies_the_port(graph) -> None:
    assert isinstance(graph, KnowledgeGraphPort)


# ── validity intervals ───────────────────────────────────────────────────────


async def test_a_statement_is_visible_while_valid(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER
    )

    found = await graph.query_entity("alice", audiences=(OWNER,))

    assert [(r.subject, r.predicate, r.object) for r in found] == [
        ("alice", "lives_in", "berlin")
    ]


async def test_invalidating_keeps_the_row_and_hides_it_from_now(graph) -> None:
    """History has to stay answerable — they *did* live there."""

    await graph.add_triple(
        subject="alice",
        predicate="lives_in",
        object="berlin",
        audience=OWNER,
        valid_from="2020-01-01",
    )
    changed = await graph.invalidate(
        subject="alice", predicate="lives_in", object="berlin", ended="2024-01-01"
    )

    assert changed == 1
    assert await graph.query_entity("alice", audiences=(OWNER,)) == []

    earlier = await graph.query_entity("alice", audiences=(OWNER,), as_of="2022-06-01")
    assert [r.object for r in earlier] == ["berlin"]


async def test_invalidating_twice_changes_nothing_the_second_time(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER
    )

    assert await graph.invalidate(
        subject="alice", predicate="lives_in", object="berlin"
    ) == 1
    assert await graph.invalidate(
        subject="alice", predicate="lives_in", object="berlin"
    ) == 0


async def test_invalidating_something_absent_is_reported_not_raised(graph) -> None:
    """Zero rows is a legitimate answer, not a failure."""

    assert await graph.invalidate(
        subject="nobody", predicate="lives_in", object="nowhere"
    ) == 0


async def test_superseding_leaves_no_gap(graph) -> None:
    """At the boundary instant exactly one of the two statements is true.

    Doing this as invalidate-then-add would leave a moment where a reader sees
    the fact as absent entirely.
    """

    await graph.add_triple(
        subject="alice",
        predicate="lives_in",
        object="berlin",
        audience=OWNER,
        valid_from="2020-01-01",
    )
    await graph.supersede(
        subject="alice",
        predicate="lives_in",
        old_object="berlin",
        new_object="lisbon",
        audience=OWNER,
        changed_at="2024-01-01",
    )

    at_boundary = await graph.query_entity(
        "alice", audiences=(OWNER,), as_of="2024-01-01"
    )
    assert [r.object for r in at_boundary] == ["lisbon"]

    before = await graph.query_entity("alice", audiences=(OWNER,), as_of="2023-12-31")
    assert [r.object for r in before] == ["berlin"]


async def test_a_date_only_value_is_widened_on_write(graph) -> None:
    """So the interval test is plain SQL rather than a per-query length check."""

    assert canonical_temporal("2026-01-01") == "2026-01-01T00:00:00Z"

    await graph.add_triple(
        subject="alice",
        predicate="lives_in",
        object="berlin",
        audience=OWNER,
        valid_from="2026-01-01",
    )
    found = await graph.query_entity(
        "alice", audiences=(OWNER,), as_of="2026-01-01T00:00:01Z"
    )

    assert [r.object for r in found] == ["berlin"]


async def test_an_unparseable_timestamp_does_not_lose_the_statement(graph) -> None:
    """Callers upstream include an LLM; a bad date should not cost us the fact."""

    statement_id = await graph.add_triple(
        subject="alice",
        predicate="lives_in",
        object="berlin",
        audience=OWNER,
        valid_from="last tuesday",
    )

    assert await graph.has_triple(statement_id)


# ── audience ─────────────────────────────────────────────────────────────────


async def test_a_companions_statement_is_invisible_to_another(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="called_by", object="frog prince",
        audience=COMPANION_A,
    )

    assert await graph.query_entity("alice", audiences=BOTH_FOR_A)
    assert await graph.query_entity("alice", audiences=(OWNER, COMPANION_B)) == []


async def test_the_owner_layer_is_visible_to_every_companion(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER
    )

    for companion in (COMPANION_A, COMPANION_B):
        found = await graph.query_entity("alice", audiences=(OWNER, companion))
        assert [r.object for r in found] == ["green"]


async def test_asking_for_no_audience_returns_nothing(graph) -> None:
    """Fail closed: an empty set must not read as "no filter"."""

    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER
    )

    assert await graph.query_entity("alice", audiences=()) == []
    assert await graph.query_subjects(["alice"], audiences=()) == []
    assert await graph.timeline("alice", audiences=()) == []


# ── sensitivity ──────────────────────────────────────────────────────────────


async def test_a_sensitive_predicate_is_withheld_unless_asked_for(graph) -> None:
    """Filtered in the query, so a health fact is never read and then dropped."""

    await graph.add_triple(
        subject="alice",
        predicate="has_health_condition",
        object="asthma",
        audience=OWNER,
    )

    assert await graph.query_entity("alice", audiences=(OWNER,)) == []

    opted_in = await graph.query_entity(
        "alice", audiences=(OWNER,), include_sensitive=True
    )
    assert [r.object for r in opted_in] == ["asthma"]


async def test_sensitivity_can_be_set_explicitly(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        sensitive=True,
    )

    assert await graph.query_entity("alice", audiences=(OWNER,)) == []


# ── idempotency ──────────────────────────────────────────────────────────────


async def test_replaying_a_turn_does_not_duplicate(graph) -> None:
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


async def test_replaying_a_turn_does_not_undo_a_later_invalidation(graph) -> None:
    """The awkward case: add, change your mind, then the turn is redelivered.

    Checking the source id before the validity rule is what makes this work — the
    other order would re-add the fact as newly valid.
    """

    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-1",
    )
    await graph.invalidate(subject="alice", predicate="likes", object="green")

    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-1",
    )

    assert await graph.query_entity("alice", audiences=(OWNER,)) == []


async def test_the_same_fact_from_a_different_turn_is_not_re_added(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-1",
    )
    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-2",
    )

    assert (await graph.stats())["triples_active"] == 1


async def test_a_fact_can_hold_again_after_being_ended(graph) -> None:
    """Someone moves away and back; those are two intervals, not one row."""

    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER,
        valid_from="2018-01-01",
    )
    await graph.invalidate(
        subject="alice", predicate="lives_in", object="berlin", ended="2020-01-01"
    )
    await graph.add_triple(
        subject="alice", predicate="lives_in", object="berlin", audience=OWNER,
        valid_from="2024-01-01",
    )

    stats = await graph.stats()
    assert stats["triples_total"] == 2
    assert stats["triples_active"] == 1


# ── bounded reads ────────────────────────────────────────────────────────────


async def test_subjects_are_bounded_individually(graph) -> None:
    """One well-connected entity must not crowd the others out of the budget."""

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
    by_subject: dict[str, int] = {}
    for record in found:
        by_subject[record.subject] = by_subject.get(record.subject, 0) + 1

    assert by_subject == {"alice": 2, "bob": 1}


async def test_query_subjects_only_follows_outgoing_edges(graph) -> None:
    """The recall read asks what a subject asserts, not what points at it."""

    await graph.add_triple(
        subject="bob", predicate="is_parent_of", object="alice", audience=OWNER
    )

    assert await graph.query_subjects(["alice"], audiences=(OWNER,)) == []
    assert await graph.query_subjects(["bob"], audiences=(OWNER,))


async def test_direction_selects_which_side_the_entity_is_on(graph) -> None:
    await graph.add_triple(
        subject="bob", predicate="is_parent_of", object="alice", audience=OWNER
    )

    assert await graph.query_entity(
        "alice", audiences=(OWNER,), direction="outgoing"
    ) == []
    assert await graph.query_entity("alice", audiences=(OWNER,), direction="incoming")
    assert await graph.query_entity("alice", audiences=(OWNER,), direction="both")


async def test_an_unknown_direction_is_rejected(graph) -> None:
    with pytest.raises(ValueError, match="outgoing|incoming|both"):
        await graph.query_entity("alice", audiences=(OWNER,), direction="sideways")


# ── entity naming and aliases ────────────────────────────────────────────────


async def test_names_differing_only_in_case_are_one_entity(graph) -> None:
    """LLM output is inconsistent about casing; the slug absorbs that."""

    assert entity_id_for("My Dad") == entity_id_for("my dad")

    await graph.add_triple(
        subject="My Dad", predicate="likes", object="tea", audience=OWNER
    )

    assert await graph.query_entity("my dad", audiences=(OWNER,))


async def test_an_alias_resolves_to_its_entity(graph) -> None:
    await graph.add_triple(
        subject="robert", predicate="likes", object="tea", audience=OWNER
    )
    await graph.record_entity_mention(
        entity_id=entity_id_for("robert"), alias="my dad", source="steward"
    )

    matched = await graph.match_entities_for_query("what does my dad like", cap=3)

    assert "robert" in matched


async def test_entity_matching_is_bounded(graph) -> None:
    for name in ("alice", "bob", "carol"):
        await graph.add_triple(
            subject=name, predicate="likes", object="tea", audience=OWNER
        )

    matched = await graph.match_entities_for_query("alice bob carol", cap=2)

    assert len(matched) == 2


async def test_matching_an_empty_query_finds_nothing(graph) -> None:
    assert await graph.match_entities_for_query("", cap=3) == []
    assert await graph.match_entities_for_query("anything", cap=0) == []


# ── probes and stats ─────────────────────────────────────────────────────────


async def test_a_pending_write_can_be_located_by_its_turn(graph) -> None:
    """How the write tools poll for an outcome without retrying the write."""

    statement_id = await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        source_turn_id="turn-1",
    )

    assert await graph.find_pending_triple_id(
        "turn-1", "alice", "likes", "green"
    ) == statement_id
    assert await graph.find_pending_triple_id(
        "turn-2", "alice", "likes", "green"
    ) is None


async def test_an_applied_invalidation_can_be_confirmed(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER
    )

    assert not await graph.find_invalidation_applied(
        "alice", "likes", "green", "2030-01-01"
    )

    await graph.invalidate(
        subject="alice", predicate="likes", object="green", ended="2026-01-01"
    )

    assert await graph.find_invalidation_applied(
        "alice", "likes", "green", "2026-01-02"
    )


async def test_stats_separates_active_from_ended(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER
    )
    await graph.add_triple(
        subject="alice", predicate="likes", object="tea", audience=OWNER
    )
    await graph.invalidate(subject="alice", predicate="likes", object="tea")

    stats = await graph.stats()

    assert stats["triples_total"] == 2
    assert stats["triples_active"] == 1
    assert stats["triples_invalidated"] == 1
    assert stats["entities"] == 3


async def test_closing_twice_is_harmless(tmp_path) -> None:
    graph = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id="alice", lock=SpaceLock()
    )

    graph.close()
    graph.close()


async def test_a_graph_reopens_with_its_statements(tmp_path) -> None:
    """Switching the graph off and on again must not lose anything."""

    path = tmp_path / "kg.sqlite3"
    first = SqliteKnowledgeGraph(path, space_id="alice", lock=SpaceLock())
    await first.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER
    )
    first.close()

    second = SqliteKnowledgeGraph(path, space_id="alice", lock=SpaceLock())
    try:
        assert [r.object for r in await second.query_entity("alice", audiences=(OWNER,))] == [
            "green"
        ]
    finally:
        second.close()


async def test_two_spaces_in_one_file_cannot_see_each_other(tmp_path) -> None:
    """The space column is defence in depth, and it has to actually work.

    Locally each space has its own file so this is redundant — which is exactly
    why it needs a test: nothing else would notice if the column were ignored,
    and a shared database depends on it entirely.
    """

    path = tmp_path / "shared.sqlite3"
    lock = SpaceLock()
    alice = SqliteKnowledgeGraph(path, space_id="alice", lock=lock)
    bob = SqliteKnowledgeGraph(path, space_id="bob", lock=lock)
    try:
        await alice.add_triple(
            subject="alice", predicate="likes", object="green", audience=OWNER
        )

        assert await alice.query_entity("alice", audiences=(OWNER,))
        assert await bob.query_entity("alice", audiences=(OWNER,)) == []
        assert (await bob.stats())["triples_total"] == 0
    finally:
        bob.close()
        alice.close()


# ── ordering and concurrency ──────────────────────────────────────────────────


async def test_the_timeline_runs_newest_first(graph) -> None:
    """Operators reading a timeline want the latest state at the top."""

    await graph.add_triple(
        subject="alice", predicate="attended", object="event-a", audience=OWNER,
        valid_from="2025-01-01T00:00:00Z",
    )
    await graph.add_triple(
        subject="alice", predicate="attended", object="event-b", audience=OWNER,
        valid_from="2026-01-01T00:00:00Z",
    )

    rows = await graph.timeline("alice", audiences=(OWNER,), limit=10)

    assert [r.object for r in rows][:2] == ["event-b", "event-a"]


async def test_the_timeline_can_be_bounded_to_a_window(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="attended", object="old", audience=OWNER,
        valid_from="2020-01-01T00:00:00Z",
    )
    await graph.add_triple(
        subject="alice", predicate="attended", object="recent", audience=OWNER,
        valid_from="2026-01-01T00:00:00Z",
    )

    rows = await graph.timeline(
        "alice", audiences=(OWNER,), since="2025-01-01T00:00:00Z", limit=10
    )

    assert [r.object for r in rows] == ["recent"]


async def test_concurrent_writes_do_not_collide(graph) -> None:
    """The shared lock is what keeps SQLite from reporting a busy database."""

    await asyncio.gather(
        *(
            graph.add_triple(
                subject="alice", predicate="likes", object=drink, audience=OWNER,
                source_turn_id=f"turn-{drink}",
            )
            for drink in ("tea", "wine", "coffee")
        )
    )

    assert (await graph.stats())["triples_total"] == 3


async def test_a_python_utc_timestamp_is_normalised(graph) -> None:
    """Callers upstream send datetime.isoformat(), which has microseconds."""

    assert canonical_temporal("2026-06-28T11:41:17.964620+00:00") == "2026-06-28T11:41:17Z"

    await graph.add_triple(
        subject="alice", predicate="likes", object="green", audience=OWNER,
        valid_from="2026-06-28T11:41:17.964620+00:00",
    )
    found = await graph.query_entity("alice", audiences=(OWNER,))

    assert found[0].valid_from == "2026-06-28T11:41:17Z"


async def test_entity_names_are_listed_once_each(graph) -> None:
    await graph.add_triple(
        subject="alice", predicate="likes", object="tea", audience=OWNER
    )
    await graph.add_triple(
        subject="alice", predicate="likes", object="wine", audience=OWNER
    )

    names = await graph.list_entity_names()

    assert names.count("alice") == 1


# ── alias resolution order ────────────────────────────────────────────────────


async def test_the_longest_matching_alias_wins(graph) -> None:
    """"我老婆" must beat the substring "老婆" it contains."""

    await graph.add_triple(
        subject="li", predicate="likes", object="tea", audience=OWNER
    )
    await graph.add_triple(
        subject="wang", predicate="likes", object="wine", audience=OWNER
    )
    await graph.record_entity_mention(
        entity_id=entity_id_for("li"), alias="老婆", source="steward"
    )
    await graph.record_entity_mention(
        entity_id=entity_id_for("wang"), alias="我老婆", source="steward"
    )

    matched = await graph.match_entities_for_query("我老婆喜欢什么", cap=1)

    assert matched == ["wang"]


async def test_a_canonical_match_is_not_repeated_via_its_alias(graph) -> None:
    await graph.add_triple(
        subject="robert", predicate="likes", object="tea", audience=OWNER
    )
    await graph.record_entity_mention(
        entity_id=entity_id_for("robert"), alias="robert", source="steward"
    )

    matched = await graph.match_entities_for_query("what does robert like", cap=5)

    assert matched.count("robert") == 1


async def test_matching_works_with_no_aliases_recorded(graph) -> None:
    await graph.add_triple(
        subject="robert", predicate="likes", object="tea", audience=OWNER
    )

    assert await graph.match_entities_for_query("robert", cap=3) == ["robert"]


async def test_a_prefixed_entity_matches_its_bare_name(graph) -> None:
    """The steward writes "pet:铁锤" to disambiguate; a person says 铁锤."""

    await graph.add_triple(
        subject="pet:铁锤", predicate="holds_role", object="dog", audience=OWNER
    )

    assert await graph.match_entities_for_query("铁锤是什么", cap=3) == ["pet:铁锤"]


async def test_a_bare_prefix_matches_nothing(graph) -> None:
    """Otherwise a broken name would fire on any query containing a colon."""

    await graph.add_triple(
        subject="pet:", predicate="holds_role", object="dog", audience=OWNER
    )

    assert await graph.match_entities_for_query("what about pet: things", cap=3) == [
        "pet:"
    ]


# ── the matching rule now exists twice ────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "query"),
    [
        ("铁锤", "铁锤会说话吗"),            # whole name
        ("pet:铁锤", "铁锤会说话吗"),         # tail after the type prefix
        ("mother:张丽", "张丽住在哪"),
        ("mother:张丽", "mother:张丽 是谁"),  # whole name including the prefix
        ("铁锤", "完全无关的问题"),           # no match
        ("pet:", "关于 pet: 的事"),           # bare prefix — whole-name branch fires
        ("pet:", "没有冒号的问题"),
        ("Alice", "alice 是谁"),              # case-sensitive: must NOT match
        ("Alice", "Alice 是谁"),
        ("老婆", "我老婆叫什么"),
    ],
)
async def test_sql_matching_agrees_with_the_python_rule(stored, query, graph) -> None:
    """``match_entities_for_query`` moved into SQL; ``name_appears_in`` still
    states the rule.

    The rule is written twice now — once as Python, once as ``instr()`` in the
    query — so the two are compared here rather than assumed to agree. Case is the
    one most likely to drift: SQLite's ``LIKE`` is case-insensitive for ASCII,
    which is why the SQL uses ``instr()``, and a future edit back to ``LIKE`` would
    pass every other case in this list.
    """

    from eidolon.memory.adapters.kg_sql import name_appears_in

    await graph.add_triple(
        subject=stored, predicate="holds_role", object="x", audience=OWNER
    )
    matched = stored in await graph.match_entities_for_query(query, cap=5)

    assert matched == name_appears_in(stored, query), (
        f"SQL and name_appears_in disagree on {stored!r} in {query!r}: "
        f"SQL says {matched}, Python says {name_appears_in(stored, query)}"
    )


async def test_longer_names_still_win(graph) -> None:
    """``mother:张丽`` must beat a bare ``mother`` when both could fire — the
    ordering moved into the query's ``ORDER BY length(name) DESC`` and is easy to
    drop while everything else keeps working."""

    for name in ("mother", "mother:张丽"):
        await graph.add_triple(
            subject=name, predicate="holds_role", object="x", audience=OWNER
        )

    assert await graph.match_entities_for_query("mother:张丽 住在哪", cap=1) == [
        "mother:张丽"
    ]


async def test_a_soft_forget_ends_only_what_is_still_valid(graph) -> None:
    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience=OWNER, source_turn_id="turn-1",
    )
    await graph.add_triple(
        subject="用户", predicate="likes", object="咖啡",
        audience=OWNER, source_turn_id="turn-2",
    )

    assert await graph.forget_source_turns(["turn-1"]) == 1
    # Nothing left valid from that turn, so a repeat counts nothing — the shape a
    # retry after a partial failure takes.
    assert await graph.forget_source_turns(["turn-1"]) == 0

    objects = {r.object for r in await graph.query_entity("用户", audiences=(OWNER,))}
    assert objects == {"咖啡"}


async def test_a_soft_forget_keeps_the_row_answerable(graph) -> None:
    """Ending an interval is not deleting: the history is still there to read."""

    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience=OWNER, source_turn_id="turn-1",
    )
    await graph.forget_source_turns(["turn-1"])

    stats = await graph.stats()
    assert stats["triples_total"] == 1
    assert stats["triples_active"] == 0
    assert stats["triples_invalidated"] == 1


async def test_an_unknown_turn_is_not_an_error(graph) -> None:
    assert await graph.forget_source_turns(["never-happened"]) == 0
    assert await graph.forget_source_turns(["never-happened"], hard=True) == 0
    assert await graph.forget_source_turns([]) == 0
    assert await graph.forget_source_turns(["", "   "]) == 0


async def test_a_hard_forget_refuses_rather_than_delete_unrecorded(graph, tmp_path) -> None:
    """The rule the port states: no record, no deletion.

    A deletion that cannot be written down is the failure this whole path exists
    to prevent, so it has to raise rather than proceed and log a warning. Blocked
    here by putting a file where the log directory needs to be, which is the
    cheapest stand-in for the disk being full or read-only.
    """

    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience=OWNER, source_turn_id="turn-1",
    )
    (tmp_path / "forgotten").write_text("in the way", encoding="utf-8")

    with pytest.raises(OSError):
        await graph.forget_source_turns(["turn-1"], hard=True)

    # Still there. The point of refusing is that nothing was lost.
    assert (await graph.stats())["triples_total"] == 1
    objects = {r.object for r in await graph.query_entity("用户", audiences=(OWNER,))}
    assert objects == {"绿茶"}


async def test_a_hard_forget_keeps_appending_to_one_day(graph, tmp_path) -> None:
    """Two forgets on the same day are two lines, not one file overwriting another."""

    for index, turn in enumerate(("turn-1", "turn-2")):
        await graph.add_triple(
            subject="用户", predicate="likes", object=f"茶{index}",
            audience=OWNER, source_turn_id=turn,
        )
    await graph.forget_source_turns(["turn-1"], hard=True)
    await graph.forget_source_turns(["turn-2"], hard=True)

    exports = sorted((tmp_path / "forgotten").glob("*.jsonl"))
    assert len(exports) == 1
    lines = [l for l in exports[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert (await graph.stats())["triples_total"] == 0


async def test_a_hard_forget_leaves_the_entities(graph) -> None:
    """Stated because it is a limit on the promise, not an oversight.

    An entity can be named by statements from turns nobody asked to forget, and
    proving otherwise costs a query per entity. Collecting orphans belongs to a
    sweep. So a hard forget removes what was said, not the fact that a name was
    once known.
    """

    await graph.add_triple(
        subject="张丽", predicate="lives_in", object="杭州",
        audience=OWNER, source_turn_id="turn-1",
    )
    before = (await graph.stats())["entities"]

    await graph.forget_source_turns(["turn-1"], hard=True)

    assert (await graph.stats())["triples_total"] == 0
    assert (await graph.stats())["entities"] == before


async def test_an_alias_resolves_when_the_caller_passes_a_display_name(graph) -> None:
    """What production actually passes, which no test passed before.

    ``turn_processor`` hands ``record_entity_mention`` the steward's entity name
    verbatim; every test here called ``entity_id_for`` first. So the tests agreed
    with each other and with nothing else, and the one write on the port that did
    not normalise its key went unnoticed — the row was written, counted, and
    unreachable, because the read joins mentions to entities on that column.
    """

    await graph.add_triple(
        subject="My Dad", predicate="likes", object="tea", audience=OWNER
    )
    await graph.record_entity_mention(
        entity_id="My Dad", alias="老爸", source="steward"
    )

    assert "My Dad" in await graph.match_entities_for_query("老爸喜欢什么", cap=3)


async def test_the_display_name_and_its_slug_are_the_same_mention(graph) -> None:
    """Two spellings of one entity must not become two rows.

    ``mention_id`` is derived from the id, so normalising it after the fact would
    quietly split a mention in two if the derivation were left alone.
    """

    await graph.add_triple(
        subject="Dr. Li", predicate="works_at", object="clinic", audience=OWNER
    )
    await graph.record_entity_mention(entity_id="Dr. Li", alias="医生", source="steward")
    await graph.record_entity_mention(
        entity_id=entity_id_for("Dr. Li"), alias="医生", source="steward"
    )

    assert (await graph.stats())["mentions"] == 1


@pytest.mark.parametrize(
    "name",
    [
        "My Dad",          # a space
        "Dr. Li",          # punctuation
        "  Mom  ",         # padding
        "mother:张丽",     # already a slug — the case that hid the bug
        "铁锤",            # ditto
    ],
)
async def test_every_name_shape_reaches_its_entity(graph, name: str) -> None:
    """Latin-script names were broken and Chinese ones were not.

    The corpus is Chinese, and a Chinese name has no case and no spaces, so it
    equals its own slug and the join happened to work. That is the whole reason
    this shipped: the feature was correct by coincidence for the only inputs
    anyone tried.
    """

    await graph.add_triple(
        subject=name, predicate="likes", object="tea", audience=OWNER
    )
    await graph.record_entity_mention(entity_id=name, alias="昵称", source="steward")

    matched = await graph.match_entities_for_query("昵称喜欢什么", cap=3)

    assert matched, f"alias written against {name!r} resolved to nothing"


async def test_writing_to_a_hot_subject_is_a_lookup_not_a_scan(graph) -> None:
    """``add_triple``'s idempotency probes must not cost the subject's history.

    Two probes run before every insert, both keyed on the whole triple. Without
    ``object_id`` in the index SQLite finds every row for the subject and
    predicate and filters the rest by hand, so writing about "用户" — the subject
    of most of a companion's graph — grows with everything ever said about them.
    Measured on a Pi 5 at 15 199 rows under one subject and predicate: 13.4 ms
    per probe, 28.4 ms per write, on the turn path.

    Asserted as a query plan rather than a duration, because a timing threshold
    on a shared runner is a flaky test and the plan is the actual claim.
    """

    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶", audience=OWNER
    )

    plan = " ".join(
        row[-1]
        for row in graph._connection().execute(
            """
            EXPLAIN QUERY PLAN
            SELECT statement_id FROM kg_statements
            WHERE space_id = ? AND subject_id = ? AND predicate = ? AND object_id = ?
              AND valid_to IS NULL
            LIMIT 1
            """,
            ("alice", entity_id_for("用户"), "likes", entity_id_for("绿茶")),
        )
    )

    assert "object_id=?" in plan, f"the dedup probe is scanning, not looking up: {plan}"


async def test_the_old_three_column_index_is_gone(graph) -> None:
    """Renaming was the migration, so the rename has to actually take effect.

    ``CREATE INDEX IF NOT EXISTS`` matches on name alone. Had the column list
    changed under the old name, every graph that already existed would have kept
    the slow index and skipped the new statement without a word.
    """

    names = {
        row[0]
        for row in graph._connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }

    assert "idx_kg_statements_triple" in names
    assert "idx_kg_statements_subject" not in names


async def test_an_existing_graph_picks_up_the_new_index(tmp_path) -> None:
    """The case the rename exists for: a database built before the fix."""

    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        "CREATE TABLE kg_statements (space_id TEXT, statement_id TEXT, subject_id TEXT,"
        " predicate TEXT, object_id TEXT, audience TEXT, sensitive INTEGER,"
        " valid_from TEXT, valid_to TEXT, recorded_at TEXT, confidence REAL,"
        " source_turn_id TEXT, adapter_name TEXT, PRIMARY KEY (space_id, statement_id))"
    )
    legacy.execute(
        "CREATE INDEX idx_kg_statements_subject"
        " ON kg_statements (space_id, subject_id, predicate)"
    )
    legacy.commit()
    legacy.close()

    graph = SqliteKnowledgeGraph(path, space_id="alice", lock=SpaceLock())
    try:
        names = {
            row[0]
            for row in graph._connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_kg_statements_triple" in names
        assert "idx_kg_statements_subject" not in names
    finally:
        graph.close()


async def test_matching_is_a_seek_not_a_scan(graph) -> None:
    """The claim that turned the question around, asserted as a plan.

    A duration would be flaky on a shared runner; the plan is the actual claim —
    both lookups must be covering-index seeks on an equality, not a walk of the
    space.
    """

    await graph.add_triple(
        subject="pet:铁锤", predicate="holds_role", object="dog", audience=OWNER
    )
    connection = graph._connection()

    whole = " ".join(
        row[-1]
        for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT name FROM kg_entities "
            "WHERE space_id = ? AND name <> '' AND name IN (?, ?) "
            "ORDER BY length(name) DESC LIMIT ?",
            ("alice", "铁锤", "x", 3),
        )
    )
    tail = " ".join(
        row[-1]
        for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT name FROM kg_entities "
            "WHERE space_id = ? AND instr(name, ':') > 0 "
            "  AND substr(name, instr(name, ':') + 1) IN (?, ?) "
            "ORDER BY length(name) DESC LIMIT ?",
            ("alice", "铁锤", "x", 3),
        )
    )

    assert "COVERING INDEX idx_kg_entities_name" in whole, whole
    assert "name=?" in whole, whole
    assert "COVERING INDEX idx_kg_entities_tail" in tail, tail
    assert "<expr>=?" in tail, tail


@pytest.mark.parametrize(
    "phrase",
    [
        "我妈妈住在哪里",
        "铁锤是什么品种的狗",
        "what does My Dad like",
        "老王和张丽是同事吗",
        "",
        "：",
        "pet:",
        "a",
        "重复重复重复重复重复",
        "混合 mixed 中英 text 铁锤 and My Dad together",
    ],
)
async def test_the_lookup_finds_exactly_what_the_rule_says(graph, phrase: str) -> None:
    """Equivalence, over a population rather than a handful of examples.

    Enumerating the phrase's substrings and seeking them is only a valid rewrite
    of "which stored names occur in this phrase" if it returns the same set. The
    Python rule is the specification; every stored name is checked against it and
    the two answers must agree exactly — not overlap, not approximate.
    """

    from eidolon.memory.adapters.kg_sql import name_appears_in

    stored = [
        "铁锤", "pet:铁锤", "mother:张丽", "张丽", "老王", "My Dad", "my dad",
        "pet:", "重复", "a", "mixed", "同事", "妈妈", "住在", "：冒号",
    ]
    for index, name in enumerate(stored):
        await graph.add_triple(
            subject=name, predicate="likes", object=f"o{index}", audience=OWNER
        )

    # Derived from what the table actually holds, not from the list above:
    # ``entity_id_for`` slugs "My Dad" and "my dad" to one id, so only the first
    # spelling becomes a row. Computing the expectation from the input would
    # assert against entities that do not exist.
    present = [row["name"] for row in graph._connection().execute(
        "SELECT name FROM kg_entities WHERE space_id = ?", ("alice",)
    )]
    expected = {name for name in present if name_appears_in(name, phrase)}
    # ``cap`` above the corpus so truncation cannot mask a disagreement.
    actual = set(await graph.match_entities_for_query(phrase, cap=len(present) + 5))

    assert actual == expected, f"{phrase!r}: extra={actual - expected} missing={expected - actual}"
