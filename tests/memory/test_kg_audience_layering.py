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

So the blocker today is **nothing marks a statement as private**. There is no
intent, no field, and no product surface where a person says "keep this between
us"; the memory-governance UI that would offer it is Phase 4. Finishing the write
side before that exists would create a private layer that nothing can put
anything into, and a steward judgement with no signal to judge on.

This file therefore still pins the write side as owner-only — for the new reason.
Delete it, with the production writes it guards, on the day a marking path lands:
not when the space became per-owner (that has already happened), but when
something can say which statements are private.

``docs/ARCHITECTURE.md`` lists 写入侧 audience 归层 under 未完成 with the blocker
"需要 steward 逐条判断". That is the right blocker for the wrong reason: the cost is
not the judgement, it is that the judgement has no signal to act on — nobody can
yet tell the Host that something is private.
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
    """The pin. Nothing marks a statement private yet, so owner is the only layer."""

    offenders: list[str] = []
    for relative in _WRITE_SITES:
        path = _SOURCE_ROOT / relative
        for expression in _audience_arguments(path):
            if expression != "OWNER_AUDIENCE":
                offenders.append(f"{relative}: audience={expression}")

    assert not offenders, (
        "a production path now writes a non-owner audience:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe space is already per-owner, so the axis is real — but nothing "
        "marks a statement private yet. If a marking path has landed (an intent, "
        "a field, a person saying 'keep this between us'), delete this test "
        "together with the assumption it records."
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
