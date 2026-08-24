"""Every production write puts a statement in the owner layer. On purpose.

The read side of the audience axis is fully built: ``audience`` is a column, the
filter is an ``IN`` clause in SQL rather than a pass over the results, there is no
wildcard token, and an empty audience set returns nothing instead of everything.
The write side always supplies one value.

That reads like a half-finished feature and is not — but **the reason has
changed, and the old one is no longer true**.

*It used to be:* a memory space was ``(tenant, owner, companion)``, so a space
held exactly one companion, there was nothing to leak and nothing to share, and
writing ``companion:<id>`` into a single-companion palace would change no
observable behaviour.

*It is now:* a space is per-**owner** (ratified 2026-08-23,
``docs/跨系统/多Companion记忆隔离机制裁决.md``; the schema change landed in
``eidolon_data@13d7858``). One palace holds every Companion's statements, and the
audience axis is the only thing that could separate them. The read side applies
it on both production paths — see ``test_companion_audience_isolation.py``, which
proves a companion-layer statement is invisible to another Companion through the
recall code rather than only through the storage adapter.

*And the reason has changed again.* A marking path has landed: the Owner can say
"只让它记得" about an exact memory, and the command that applies it moves the
drawer **and** the statements extracted from its turn
(``test_owner_audience_http.py``, ``test_audience_marking.py``). So the write
side is no longer owner-only in every sense, and this file no longer claims it
is. What it pins now is narrower and still worth pinning:

**Extraction creates statements in the owner layer. Only a person moves one out.**

That is the invariant with teeth. An extractor deciding on its own that something
is private to one Companion would be a steward judgement with no signal to judge
on — and it would be invisible, because a statement quietly in the wrong layer
reads exactly like a statement nobody said. So the four production ``add_triple``
sites are still checked to pass ``OWNER_AUDIENCE``, and the one path that moves a
statement afterwards is named below, so a second one is a decision someone makes
here too.

``docs/ARCHITECTURE.md`` lists 写入侧 audience 归层 under 未完成 with the blocker
"需要 steward 逐条判断". The blocker was never the judgement; it was that the
judgement had no signal to act on. The signal is now a person saying so.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from eidolon_memory_contracts import OWNER_AUDIENCE, companion_audience, readable_audiences

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.domain.space_lock import SpaceLock

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "eidolon/memory"

#: Where a triple is written on a production path. Kept as a list rather than
#: discovered, so adding a fifth write site is a decision someone makes here too.
_WRITE_SITES = (
    "application/turn_processor.py",
    "application/commitments.py",
    "application/explicit_intents.py",
)


#: Which calls this reads. It used to be "any call with an ``audience=``
#: keyword", which was the same set for as long as the only such calls were
#: writes — and then stopped being: a log line naming the audience it had just
#: applied tripped it. A detector that answers a wider question than its name
#: fails on things that are fine, which is how a pin stops being trusted.
_TRIPLE_WRITES = ("add_triple", "add_statement")


def _audience_arguments(path: Path) -> list[str]:
    """Every ``audience=`` passed where a statement is created."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
        if name not in _TRIPLE_WRITES:
            continue
        for keyword in node.keywords:
            if keyword.arg != "audience":
                continue
            found.append(ast.unparse(keyword.value))
    return found


def test_no_production_path_creates_a_statement_outside_the_owner_layer() -> None:
    """The pin. A statement is born owner-wide; only a person moves it."""

    offenders: list[str] = []
    for relative in _WRITE_SITES:
        path = _SOURCE_ROOT / relative
        for expression in _audience_arguments(path):
            if expression != "OWNER_AUDIENCE":
                offenders.append(f"{relative}: audience={expression}")

    assert not offenders, (
        "a production path now creates a statement outside the owner layer:\n  "
        + "\n  ".join(offenders)
        + "\n\nMoving a statement to one Companion is a person's decision and has "
        "a path of its own (see the test below). Deciding it at extraction time "
        "is a steward judgement with no signal to judge on, and a statement "
        "quietly in the wrong layer reads exactly like one nobody said."
    )


def test_the_write_sites_are_all_still_here() -> None:
    """Otherwise the pin above passes by finding nothing to check."""

    total = sum(len(_audience_arguments(_SOURCE_ROOT / rel)) for rel in _WRITE_SITES)

    assert total >= 4, f"expected the four known audience writes, found {total}"


# ── and the read side that is already waiting for it ──────────────────────────


async def test_the_read_side_already_separates_the_two_layers(tmp_path) -> None:
    """Built, tested, and currently fed one value.

    Worth asserting alongside the pin: what is missing is a *marking path*, not
    a mechanism. The end-to-end version of this — through the recall code rather
    than the adapter — is in ``test_companion_audience_isolation.py``.
    """

    graph = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id="default.alice.default", lock=SpaceLock()
    )
    try:
        await graph.add_triple(
            subject="用户", predicate="likes", object="乌龙茶", audience=OWNER_AUDIENCE
        )
        await graph.add_triple(
            subject="用户",
            predicate="likes",
            object="密室逃脱",
            audience=companion_audience("mochi"),
        )

        # A companion sees the owner layer and its own, and nothing of another's.
        mochi = await graph.query_entity(
            "用户", audiences=readable_audiences("mochi"), direction="outgoing"
        )
        other = await graph.query_entity(
            "用户", audiences=readable_audiences("nori"), direction="outgoing"
        )

        assert {row.object for row in mochi} == {"乌龙茶", "密室逃脱"}
        assert {row.object for row in other} == {"乌龙茶"}
    finally:
        graph.close()


@pytest.mark.parametrize("companion_id", [None, ""])
async def test_an_unidentified_caller_gets_the_owner_layer_only(companion_id) -> None:
    """Fails closed. A caller who did not say who they are must not receive what
    the owner told one particular companion."""

    assert readable_audiences(companion_id) == (OWNER_AUDIENCE,)


def test_exactly_one_path_moves_a_statement_out_of_the_owner_layer() -> None:
    """Named here so a second one is a decision somebody makes in this file.

    The pin above is only meaningful alongside this: "nothing writes a companion
    audience" was easy to check and is no longer true, so what has to stay
    checkable is *how few* ways there are, and that each is a person asking.
    """

    movers = sorted(
        path.relative_to(_SOURCE_ROOT).as_posix()
        for path in _SOURCE_ROOT.rglob("*.py")
        if "move_source_turns_to_audience" in path.read_text(encoding="utf-8")
    )

    assert movers == [
        # The Owner's explicit marking, applied by the command worker.
        "adapters/kg_sqlite.py",
        "application/forget.py",
    ], movers


async def test_marking_moves_the_statements_of_that_memory_and_no_others(tmp_path) -> None:
    """The graph half of 只让它记得, at the adapter.

    Without it the drawer moves and the triples do not: the memory would vanish
    from one Eidolon's browse and still be handed to it in the next prompt, which
    is the product agreeing to keep something between two people and then telling
    the third.
    """

    graph = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id="default.alice.default", lock=SpaceLock()
    )
    try:
        await graph.add_triple(
            subject="用户",
            predicate="likes",
            object="乌龙茶",
            audience=OWNER_AUDIENCE,
            source_turn_id="turn-private",
        )
        await graph.add_triple(
            subject="用户",
            predicate="likes",
            object="密室逃脱",
            audience=OWNER_AUDIENCE,
            source_turn_id="turn-other",
        )

        moved = await graph.move_source_turns_to_audience(
            ["turn-private"], audience=companion_audience("mochi")
        )

        assert moved == 1
        mochi = await graph.query_entity(
            "用户", audiences=readable_audiences("mochi"), direction="outgoing"
        )
        nori = await graph.query_entity(
            "用户", audiences=readable_audiences("nori"), direction="outgoing"
        )
        assert {row.object for row in mochi} == {"乌龙茶", "密室逃脱"}
        # The other Eidolon keeps everything it was not asked to stop knowing.
        assert {row.object for row in nori} == {"密室逃脱"}
    finally:
        graph.close()


async def test_moving_a_statement_ends_no_interval_and_removes_nothing(tmp_path) -> None:
    """It is not a forget in a friendlier word: the statement stays true."""

    graph = SqliteKnowledgeGraph(
        tmp_path / "kg.sqlite3", space_id="default.alice.default", lock=SpaceLock()
    )
    try:
        await graph.add_triple(
            subject="用户",
            predicate="likes",
            object="乌龙茶",
            audience=OWNER_AUDIENCE,
            source_turn_id="turn-private",
        )

        await graph.move_source_turns_to_audience(
            ["turn-private"], audience=companion_audience("mochi")
        )

        rows = await graph.query_entity(
            "用户", audiences=readable_audiences("mochi"), direction="outgoing"
        )
        assert [row.object for row in rows] == ["乌龙茶"]
        assert all(row.valid_to is None for row in rows)
    finally:
        graph.close()
