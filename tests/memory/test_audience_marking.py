"""Marking a memory as one Companion's, and the other one no longer recalling it.

The audience axis has had a proven read side since Phase 2 — both production
recall legs filter by it — and no way to ask. This is the other half: the write,
and the property the whole axis exists for, checked end to end through the same
functions the command worker calls and the same policy recall uses.

Two things are deliberately *not* symmetric with a forget, and both are asserted
here rather than left as prose:

- **The statement is not touched.** A memory given to one Companion is still
  recalled, in full, by that Companion. Nothing is archived, deleted or
  rewritten; only who may see it changes.
- **It goes back.** The same call with no Companion named returns it to the Owner
  layer, where every Eidolon may recall it again. A marking that could not be
  undone would be a delete wearing a friendlier word.
"""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    MemoryActorContext,
    companion_audience,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.forget import assign_audience_to_exact_drawers
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.domain.wire import MemoryWireRecord

pytestmark = pytest.mark.asyncio

SPACE = "r_owner_one"
MOCHI = "c_mochi"
NORI = "c_nori"


def _seed(backend: FakeMemoryBackend, key: str, text: str) -> None:
    backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=key,
        value=text,
        metadata={"memory_space_id": SPACE, "wing": "Wing_Relationship"},
    )


def _context(companion_id: str | None) -> MemoryActorContext:
    """One Owner's space, asked by one of their Companions.

    The same space id in both contexts, because that is the point of the
    per-Owner decision: the audience is the only thing separating them.
    """

    return MemoryActorContext(
        memory_realm_id=SPACE,
        memory_space_id=SPACE,
        companion_id=companion_id,
    )


def _visible(record, companion_id: str | None) -> bool:
    return RecallPolicyRegistry.default().visible(
        record, context=_context(companion_id)
    )


async def test_a_marked_memory_is_recalled_by_one_companion_and_not_the_other() -> None:
    """The property the axis exists for, through the production read path."""

    backend = FakeMemoryBackend()
    _seed(backend, "drawer_joke", "我们之间那个关于乌龙茶的玩笑")
    record = await backend.get(SPACE, "drawer_joke")
    assert _visible(record, NORI), "an unmarked memory belongs to the Owner layer"

    moved = await assign_audience_to_exact_drawers(
        backend, SPACE, ["drawer_joke"], companion_audience(MOCHI)
    )

    assert moved == ["drawer_joke"]
    marked = await backend.get(SPACE, "drawer_joke")
    assert _visible(marked, MOCHI)
    assert not _visible(marked, NORI)
    # And not to a caller who did not say which Eidolon they are: that context
    # reads the Owner layer, which this memory has left.
    assert not _visible(marked, None)


async def test_marking_changes_who_may_see_it_and_nothing_else() -> None:
    """Not a forget in a friendlier word.

    If this drifted into archiving the drawer, the Companion it was given to
    would stop recalling it too — and the page that asked would still look like
    it worked.
    """
    backend = FakeMemoryBackend()
    _seed(backend, "drawer_joke", "我们之间那个关于乌龙茶的玩笑")

    await assign_audience_to_exact_drawers(
        backend, SPACE, ["drawer_joke"], companion_audience(MOCHI)
    )

    marked = await backend.get(SPACE, "drawer_joke")
    assert marked.value == "我们之间那个关于乌龙茶的玩笑"
    assert "privacy" not in marked.metadata
    assert marked.metadata["wing"] == "Wing_Relationship"


async def test_a_memory_can_be_given_back_to_every_companion() -> None:
    """The same call, with nobody named."""

    backend = FakeMemoryBackend()
    _seed(backend, "drawer_joke", "我们之间那个关于乌龙茶的玩笑")
    await assign_audience_to_exact_drawers(
        backend, SPACE, ["drawer_joke"], companion_audience(MOCHI)
    )

    await assign_audience_to_exact_drawers(
        backend, SPACE, ["drawer_joke"], OWNER_AUDIENCE
    )

    given_back = await backend.get(SPACE, "drawer_joke")
    assert _visible(given_back, NORI)
    assert _visible(given_back, MOCHI)


async def test_only_the_named_memories_move() -> None:
    """Exact ids, because the person picked these off a page they were reading."""

    backend = FakeMemoryBackend()
    _seed(backend, "drawer_joke", "我们之间那个关于乌龙茶的玩笑")
    _seed(backend, "drawer_city", "用户住在常州")

    await assign_audience_to_exact_drawers(
        backend, SPACE, ["drawer_joke"], companion_audience(MOCHI)
    )

    untouched = await backend.get(SPACE, "drawer_city")
    assert _visible(untouched, NORI)


async def test_a_key_that_is_not_a_drawer_never_reaches_the_store() -> None:
    """The same guard the two privacy writes have, and for the same reason."""

    backend = FakeMemoryBackend()
    _seed(backend, "drawer_joke", "我们之间那个关于乌龙茶的玩笑")

    with pytest.raises(ValueError):
        await assign_audience_to_exact_drawers(
            backend, SPACE, ["joke"], companion_audience(MOCHI)
        )
    with pytest.raises(ValueError):
        await assign_audience_to_exact_drawers(
            backend, SPACE, [], companion_audience(MOCHI)
        )


async def test_an_audience_the_contract_does_not_recognise_is_refused() -> None:
    """The token ends up in metadata and in vector-store filter expressions, so
    the contract that defines the axis decides what one is."""

    backend = FakeMemoryBackend()
    _seed(backend, "drawer_joke", "我们之间那个关于乌龙茶的玩笑")

    with pytest.raises(ValueError):
        await assign_audience_to_exact_drawers(
            backend, SPACE, ["drawer_joke"], "everyone"
        )


async def test_a_memory_that_is_no_longer_there_is_not_an_error() -> None:
    """It was forgotten between the page being read and the button pressed,
    which is ordinary. The command reports what moved rather than failing."""

    backend = FakeMemoryBackend()

    moved = await assign_audience_to_exact_drawers(
        backend, SPACE, ["drawer_gone"], companion_audience(MOCHI)
    )

    assert moved == []
