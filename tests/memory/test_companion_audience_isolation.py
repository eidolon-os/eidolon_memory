"""The isolation mechanism, exercised through the production read paths.

An Owner now has one memory space and every Companion shares it, so the audience
axis is the only thing that can keep one Companion's private statement from
another (``docs/跨系统/多Companion记忆隔离机制裁决.md``). Until this file, the axis
was tested at the storage adapter and at the ``readable_audiences`` helper —
both true, neither proof that the *production* recall paths apply it.

There are two of them, and a leak in either would be invisible in the other:

- the knowledge-graph leg, where the filter is an ``IN`` clause the query
  carries;
- the vector/drawer leg, where each recalled record is checked one at a time by
  ``RecallPolicyRegistry.visible``.

The statements here are written directly rather than through a write path. That
was once because no path wrote a companion audience at all; now one does — the
Owner can say "只让它记得" about an exact memory (``test_audience_marking.py``
covers that end to end, marking through the same function the command worker
calls and then checking both Companions' recall) — and writing directly is still
the right shape here, because what these tests are about is the *filter*, and a
filter tested through a writer fails for two reasons at once.

The graph leg is still filter-only: nothing marks a *triple* as one Companion's,
so a statement's audience there comes from how it was written. See
``test_kg_audience_layering.py`` for why that is deliberate.
"""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    MemoryActorContext,
    companion_audience,
    readable_audiences,
)

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.application.public_recall import _kg_path_with_timeout
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.domain.space_lock import SpaceLock

pytestmark = pytest.mark.asyncio

SPACE = "r_owner_one"
MOCHI = "c_mochi"
NORI = "c_nori"


def _context(companion_id: str | None) -> MemoryActorContext:
    """One Owner's space, asked by one of their Companions.

    The space id is the same in both contexts on purpose: that is the point of
    the per-Owner decision, and it is what makes the audience the only
    separation left.
    """

    return MemoryActorContext(
        owner_id="o_one",
        companion_id=companion_id,
        memory_realm_id=SPACE,
        memory_space_id=SPACE,
    )


async def _graph(tmp_path) -> SqliteKnowledgeGraph:
    graph = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id=SPACE, lock=SpaceLock()
    )
    await graph.add_triple(
        subject="用户",
        predicate="喜欢",
        object="乌龙茶",
        audience=OWNER_AUDIENCE,
    )
    await graph.add_triple(
        subject="用户",
        predicate="在计划",
        object="给阿力的生日礼物",
        audience=companion_audience(MOCHI),
    )
    return graph


async def _kg_objects(graph, companion_id: str | None) -> set[str]:
    rows = await _kg_path_with_timeout(
        graph,
        audiences=readable_audiences(companion_id),
        query="用户",
        max_entities=8,
        max_triples_per_entity=16,
        timeout_s=5.0,
        include_sensitive=True,
        kind="normal",
        subject_names=["用户"],
    )
    return {getattr(row, "object", None) or row["object"] for row in rows}


async def test_the_graph_leg_gives_each_companion_its_own_layer(tmp_path) -> None:
    """Through the recall path, not the adapter.

    The adapter-level version of this already passes; what it cannot tell us is
    whether the path that recall actually calls passes the audiences down. A
    leak here would be a leak in production while every storage test stayed
    green.
    """
    graph = await _graph(tmp_path)
    try:
        mochi = await _kg_objects(graph, MOCHI)
        nori = await _kg_objects(graph, NORI)
    finally:
        graph.close()

    assert "乌龙茶" in mochi and "乌龙茶" in nori, "Owner memory is shared"
    assert "给阿力的生日礼物" in mochi
    assert "给阿力的生日礼物" not in nori


async def test_an_unidentified_caller_sees_only_the_owner_layer(tmp_path) -> None:
    """Fails closed, on the production path.

    A caller that did not say which Companion it is asking for must not receive
    what the Owner told one particular Companion. This is the case a lost
    ``companion_id`` produces, which makes it the likely one rather than the
    exotic one.
    """
    graph = await _graph(tmp_path)
    try:
        anonymous = await _kg_objects(graph, None)
    finally:
        graph.close()

    assert anonymous == {"乌龙茶"}


class _Record:
    """A drawer record as the vector leg hands it to the policy."""

    def __init__(self, *, text: str, audience: str | None) -> None:
        self.text = text
        self.memory_space_id = SPACE
        self.metadata = {"memory_space_id": SPACE}
        if audience is not None:
            self.metadata["audience"] = audience
        self.extensions = {}


def _visible(record: _Record, companion_id: str | None) -> bool:
    return RecallPolicyRegistry.default().visible(
        record, context=_context(companion_id), include_private=True
    )


async def test_the_vector_leg_applies_the_same_rule_record_by_record() -> None:
    """The second read path. A filter on one leg only is not a filter.

    The graph leg pushes the audience into SQL; this one checks each record it
    got back. They have to agree, or which Companion sees a statement depends
    on which leg surfaced it.
    """
    owner_layer = _Record(text="用户喜欢乌龙茶", audience=OWNER_AUDIENCE)
    mochis = _Record(text="在计划给阿力的生日礼物", audience=companion_audience(MOCHI))

    assert _visible(owner_layer, MOCHI) and _visible(owner_layer, NORI)
    assert _visible(mochis, MOCHI)
    assert not _visible(mochis, NORI)
    assert not _visible(mochis, None)


async def test_an_absent_record_audience_is_owner_shared_but_still_needs_identity() -> None:
    """The record default must not weaken the interaction identity boundary."""
    legacy = _Record(text="用户喜欢乌龙茶", audience=None)

    assert _visible(legacy, MOCHI)
    assert _visible(legacy, NORI)
    assert not _visible(legacy, None)


async def test_the_space_is_still_checked_alongside_the_audience() -> None:
    """The audience axis replaced per-Companion spaces; it did not replace spaces.

    Two Owners are still two spaces, and a record that names another one is not
    admitted no matter what its audience says. Asserted here because the two
    checks now sit in the same function and a later simplification could drop
    one of them.
    """
    other_owner = _Record(text="别人的记忆", audience=OWNER_AUDIENCE)
    other_owner.memory_space_id = "r_owner_two"
    other_owner.metadata["memory_space_id"] = "r_owner_two"

    assert not _visible(other_owner, MOCHI)
    assert not _visible(other_owner, None)
