"""What one companion was told stays with that companion.

An owner may have several companions. Facts about the owner hold whichever one is
listening; what happened between the owner and one of them does not. The graph
already enforces this in its queries — these tests are about the vector store,
which holds the bulk of what is remembered and where the same rule has to apply.
"""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    MemoryActorContext,
    companion_audience,
    council_audience,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import recall_with_kg_fusion
from eidolon.memory.application.scope_policy import MissingInteractionIdentity
from eidolon.memory.application.steward.common import finalize_fragments
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.fragments import MemoryFragment

SPACE = "default.alice.default"
COMP_A = "comp_a"
COMP_B = "comp_b"


def _settings(**recall) -> MemorySettings:
    return MemorySettings.model_validate(
        {"kg": {"backend": "none"}, **({"recall": recall} if recall else {})}
    )


def _context(
    companion_id: str | None,
    *,
    council_id: str | None = None,
) -> MemoryActorContext:
    return MemoryActorContext(
        memory_realm_id=SPACE,
        owner_id="alice",
        companion_id=companion_id,
        council_id=council_id,
    )


def _fragment(content: str, *, audience: str, room: str = "colour") -> MemoryFragment:
    return MemoryFragment(
        memory_space_id=SPACE,
        owner_id="alice",
        audience=audience,
        source_turn_id=f"turn-{content[:8]}",
        wing="Wing_Life",
        room=room,
        content=content,
        memory_type="fact",
        importance=3,
        confidence=0.9,
    )


async def _recall(backend, companion_id: str | None, *, query: str = "colour", top_k: int = 5):
    fused = await recall_with_kg_fusion(
        backend,
        _settings(),
        query=query,
        context=_context(companion_id),
        top_k=top_k,
        kg=None,
        for_voice=False,
        palace_path=None,
    )
    return [record.value for record in fused["vector"]]


# ── the fragment field ───────────────────────────────────────────────────────


def test_a_fragment_defaults_to_the_owner_layer() -> None:
    """Defaulting narrow would hide the owner's own facts from their companions.

    That is the worse of the two failures while judging per statement is not yet
    something the steward does.
    """

    assert _fragment("likes green", audience=OWNER_AUDIENCE).audience == OWNER_AUDIENCE
    assert MemoryFragment(
        memory_space_id=SPACE,
        source_turn_id="t1",
        wing="Wing_Life",
        room="colour",
        content="likes green",
        memory_type="fact",
        importance=3,
        confidence=0.9,
    ).audience == OWNER_AUDIENCE


def test_provenance_and_visibility_are_separate_fields() -> None:
    """Which companion produced a memory is not who may recall it.

    A fact learned while talking to one companion is usually still a fact about
    the owner, so conflating the two would make every memory private by accident.
    """

    fragment = MemoryFragment(
        memory_space_id=SPACE,
        companion_id=COMP_A,
        audience=OWNER_AUDIENCE,
        source_turn_id="t1",
        wing="Wing_Life",
        room="colour",
        content="likes green",
        memory_type="fact",
        importance=3,
        confidence=0.9,
    )

    assert fragment.companion_id == COMP_A
    assert fragment.audience == OWNER_AUDIENCE


def test_model_private_does_not_hide_a_companion_scoped_interaction() -> None:
    """"Private code word" describes its audience, not a recall embargo.

    Before the audience axis existed, ``privacy=private`` was the only narrow
    switch. Keeping both flags after the runtime stamps ``companion:<id>`` makes
    the record invisible even to that companion, which is exactly what the Pi5
    E2E exposed for ordinary turns and Council turns.
    """

    fragment = MemoryFragment(
        memory_space_id=SPACE,
        source_turn_id="t-private-code",
        wing="Wing_Interaction",
        room="codeword",
        content="our private code word",
        memory_type="interaction",
        importance=5,
        confidence=0.9,
        privacy="private",
    )

    [stamped] = finalize_fragments(
        [fragment],
        steward="llm",
        context=_context(COMP_A),
        source_turn_id="t-private-code",
    )

    assert stamped.audience == companion_audience(COMP_A)
    assert stamped.privacy == "normal"
    assert stamped.metadata["privacy"] == "normal"


def test_owner_private_retains_management_only_semantics() -> None:
    fragment = MemoryFragment(
        memory_space_id=SPACE,
        source_turn_id="t-owner-private",
        wing="Wing_Profile",
        room="private",
        content="owner management-only note",
        memory_type="profile",
        importance=3,
        confidence=0.9,
        privacy="private",
    )

    [stamped] = finalize_fragments(
        [fragment],
        steward="admin",
        context=_context(None),
        source_turn_id="t-owner-private",
    )

    assert stamped.audience == OWNER_AUDIENCE
    assert stamped.privacy == "private"


def test_model_private_does_not_hide_a_council_interaction() -> None:
    fragment = MemoryFragment(
        memory_space_id=SPACE,
        source_turn_id="t-council-private",
        wing="Wing_Work",
        room="council",
        content="council participant-only decision",
        memory_type="work",
        importance=4,
        confidence=0.9,
        privacy="private",
    )

    [stamped] = finalize_fragments(
        [fragment],
        steward="llm",
        context=_context(COMP_A, council_id="planning"),
        source_turn_id="t-council-private",
    )

    assert stamped.audience == council_audience("planning")
    assert stamped.privacy == "normal"


@pytest.mark.parametrize("bad", ["everyone", "companion:", "", "  "])
def test_an_unrecognised_audience_is_refused_at_write_time(bad: str) -> None:
    """An unknown token would match no filter — written, then never recalled.

    Failing the write is the better outcome: a rejected memory is visible, a
    silently unrecallable one is not.
    """

    with pytest.raises(ValueError):
        _fragment("likes green", audience=bad)


# ── recall ───────────────────────────────────────────────────────────────────


async def test_the_owner_layer_reaches_every_companion() -> None:
    backend = FakeMemoryBackend()
    await backend.ingest_fragment(_fragment("likes the colour green", audience=OWNER_AUDIENCE))

    for companion in (COMP_A, COMP_B):
        assert await _recall(backend, companion) == ["likes the colour green"]


async def test_one_companions_memory_is_invisible_to_another() -> None:
    """The property this whole axis exists for."""

    backend = FakeMemoryBackend()
    await backend.ingest_fragment(
        _fragment("calls the owner frog prince", audience=companion_audience(COMP_A))
    )

    assert await _recall(backend, COMP_A, query="frog prince")
    assert await _recall(backend, COMP_B, query="frog prince") == []


async def test_an_unidentified_ordinary_caller_cannot_read_memory() -> None:
    """Missing interaction identity cannot degrade into Owner Shared access."""

    backend = FakeMemoryBackend()
    await backend.ingest_fragment(_fragment("likes green", audience=OWNER_AUDIENCE))
    await backend.ingest_fragment(
        _fragment("frog prince", audience=companion_audience(COMP_A), room="nickname")
    )

    with pytest.raises(MissingInteractionIdentity):
        await _recall(backend, None, query="green frog prince")


async def test_a_memory_written_before_the_field_existed_stays_recallable() -> None:
    """Absent audience means the owner layer — the same default writes take.

    Otherwise an upgrade would hide everything already stored, which reads to a
    user as their companion having forgotten them.
    """

    backend = FakeMemoryBackend()
    await backend.ingest_text(
        wing="Wing_Life",
        room="colour",
        text="likes green from before the upgrade",
        metadata={"memory_space_id": SPACE},  # no audience key at all
    )

    assert await _recall(backend, COMP_A) == ["likes green from before the upgrade"]


# ── the cost of filtering after retrieval ────────────────────────────────────


async def test_another_companions_memories_are_filtered_but_still_cost_budget() -> None:
    """Documents a known limitation, and pins the part that must not regress.

    The filter runs over the store's results rather than inside its query, so a
    row the caller cannot see still consumed one of the ``top_k`` slots. Measured
    against real chroma: asking for 5 with eight of another companion's memories
    ranking higher returns 1, not 5.

    What must hold is that nothing leaks — that part is asserted here. Fewer
    results than asked for is a loss of usefulness, not of privacy, and fixing it
    means pushing the filter into both stores' query languages and deciding what
    happens to rows written before the field existed. Until then this is stated
    rather than hidden.
    """

    backend = FakeMemoryBackend()
    for index in range(5):
        await backend.ingest_fragment(
            _fragment(
                f"private to A number {index}",
                audience=companion_audience(COMP_A),
                room=f"private-{index}",
            )
        )
    await backend.ingest_fragment(
        _fragment("owner likes the colour green", audience=OWNER_AUDIENCE)
    )

    recalled = await _recall(backend, COMP_B, query="private owner colour", top_k=5)

    assert not any("private to A" in text for text in recalled), (
        "A's memories must never reach B"
    )
    assert "owner likes the colour green" in recalled
