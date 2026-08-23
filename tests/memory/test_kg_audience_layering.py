"""Every production write puts a statement in the owner layer. On purpose.

The read side of the audience axis is fully built: ``audience`` is a column, the
filter is an ``IN`` clause in SQL rather than a pass over the results, there is no
wildcard token, and an empty audience set returns nothing instead of everything.
The write side always supplies one value.

That reads like a half-finished feature and is not. **Today a memory space *is*
``(tenant, owner, companion)``** — ``subjects.py`` derives it that way and each one
gets its own palace — so a space contains exactly one companion. There is nothing
to leak between companions and nothing to share, and writing
``companion:<id>`` into a single-companion palace would change no observable
behaviour while adding a judgement the steward has to get right on every statement.

The axis becomes real when a space is per-*owner* and one palace holds several
companions' statements. That is a data-model change with a migration.

**That decision has since been made** (2026-08-23,
``docs/跨系统/多Companion记忆隔离机制裁决.md``): a space becomes per-owner, and the
audience axis becomes the *only* thing separating one Companion's private
statements from another's. The read path now receives the identity it needs to
apply the filter (``--owner-id`` / ``--companion-id`` reach the runner). What has
not changed is the write side, which still puts every statement in the owner
layer — so this file still holds, and still fails loudly if someone finishes the
write side ahead of the migration.

So this file pins the current state as deliberate. Its job is to fail loudly when
someone decides to "finish" the write side, so that the change is made together
with the data-model change rather than ahead of it — and to be deleted, with the
production writes, when that day comes.

``docs/ARCHITECTURE.md`` lists 写入侧 audience 归层 under 未完成 with the blocker
"需要 steward 逐条判断". That is the right blocker for the wrong reason: the cost is
not the judgement, it is that the judgement has nothing to distinguish yet.
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


def _audience_arguments(path: Path) -> list[str]:
    """Every ``audience=`` keyword passed to a ``kg.add_triple``-shaped call."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "audience":
                continue
            found.append(ast.unparse(keyword.value))
    return found


def test_no_production_path_writes_a_companion_audience() -> None:
    """The pin. A space is one companion, so owner is the only correct layer."""

    offenders: list[str] = []
    for relative in _WRITE_SITES:
        path = _SOURCE_ROOT / relative
        for expression in _audience_arguments(path):
            if expression != "OWNER_AUDIENCE":
                offenders.append(f"{relative}: audience={expression}")

    assert not offenders, (
        "a production path now writes a non-owner audience:\n  "
        + "\n  ".join(offenders)
        + "\n\nThat is only correct once a memory space is per-owner rather than "
        "per-companion. If that change has landed, delete this test with the "
        "single-companion assumption it records."
    )


def test_the_write_sites_are_all_still_here() -> None:
    """Otherwise the pin above passes by finding nothing to check."""

    total = sum(len(_audience_arguments(_SOURCE_ROOT / rel)) for rel in _WRITE_SITES)

    assert total >= 4, f"expected the four known audience writes, found {total}"


# ── and the read side that is already waiting for it ──────────────────────────


async def test_the_read_side_already_separates_the_two_layers(tmp_path) -> None:
    """Built, tested, and currently fed one value.

    Worth asserting alongside the pin: the reason not to write companion
    audiences today is the data model, not a missing mechanism. When a space
    becomes per-owner this already works.
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
