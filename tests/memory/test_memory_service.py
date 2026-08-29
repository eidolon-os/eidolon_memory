"""One service instance, many spaces — the property a process used to lack.

A process used to *be* a space: handles resolved once at startup, captured by
every call site. Serving a second space meant a second process and a second
embedding model, so one owner with three companions cost three of each.

These tests are about the shape that replaces it. The important ones are not
"recall works" — other suites cover that — but that a space is a parameter:
several spaces through one instance, each seeing only its own memories, and an
unknown space failing as a routing fault rather than as an empty recall.
"""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    MemoryActorContext,
    MemoryReadContract,
    RecallPlan,
    companion_audience,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRuntime,
    SpaceLedgers,
    UnknownMemorySpace,
)

ALICE = "default.alice.default"
BOB = "default.bob.default"


class _FakeRouter:
    """A router over fake backends, one per space it is told to serve."""

    def __init__(self, spaces: list[str]) -> None:
        self._runtimes = {
            space: MemorySpaceRuntime(
                space_id=space,
                backend=FakeMemoryBackend(),
                palace_path=f"/tmp/{space}",
                kg=None,
                ledgers=SpaceLedgers(),
            )
            for space in spaces
        }
        self.resolves: list[str] = []

    def serves(self, space_id: str) -> bool:
        return space_id in self._runtimes

    async def resolve(self, space_id: str) -> MemorySpaceRuntime:
        self.resolves.append(space_id)
        if space_id not in self._runtimes:
            raise UnknownMemorySpace(space_id)
        return self._runtimes[space_id]

    def held_spaces(self) -> list[str]:
        return sorted(self._runtimes)

    async def aclose(self) -> None:
        return None


def _settings() -> MemorySettings:
    return MemorySettings.model_validate({"kg": {"backend": "none"}})


def _ctx(space: str, *, companion: str | None = "default") -> MemoryActorContext:
    owner = space.split(".")[1]
    return MemoryActorContext(
        memory_realm_id=space, owner_id=owner, companion_id=companion
    )


def _fragment(
    space: str,
    content: str,
    *,
    audience: str = OWNER_AUDIENCE,
    room: str = "colour",
) -> MemoryFragment:
    return MemoryFragment(
        memory_space_id=space,
        owner_id=space.split(".")[1],
        audience=audience,
        source_turn_id=f"turn-{content[:8]}",
        wing="Wing_Life",
        room=room,
        content=content,
        memory_type="fact",
        importance=3,
        confidence=0.9,
    )


@pytest.fixture
def service() -> MemoryService:
    return MemoryService(_FakeRouter([ALICE, BOB]), _settings())


# ── the shape ────────────────────────────────────────────────────────────────


def test_it_satisfies_the_read_contract(service) -> None:
    """The contract is what the service is, not a document beside it."""

    assert isinstance(service, MemoryReadContract)


async def test_one_instance_serves_several_spaces(service) -> None:
    """The property the old shape could not have.

    Each space's memories stay its own, and the caller's context is what decides
    which store is read — not which process took the request.
    """

    router = service._router
    await router.resolve(ALICE)  # warm both so writes land in the right store
    alice_backend = router._runtimes[ALICE].backend
    bob_backend = router._runtimes[BOB].backend
    await alice_backend.ingest_fragment(_fragment(ALICE, "alice likes green"))
    await bob_backend.ingest_fragment(_fragment(BOB, "bob likes blue"))

    alice = await service.recall_context(_ctx(ALICE), "colour", plan=RecallPlan())
    bob = await service.recall_context(_ctx(BOB), "colour", plan=RecallPlan())

    assert "alice likes green" in alice.context
    assert "alice" not in bob.context
    assert "bob likes blue" in bob.context


async def test_the_space_comes_from_the_caller_not_from_construction(service) -> None:
    """Nothing about which space is served is fixed when the service is built.

    That is what lets any replica serve any request, and what makes the number of
    spaces a process holds a deployment decision.
    """

    await service.recall_context(_ctx(ALICE), "x", plan=RecallPlan())
    await service.recall_context(_ctx(BOB), "x", plan=RecallPlan())

    assert service._router.resolves == [ALICE, BOB]


async def test_an_unserved_space_is_a_routing_fault_not_an_empty_recall(
    service,
) -> None:
    """Answering it with an empty result would read to a user as amnesia."""

    with pytest.raises(UnknownMemorySpace):
        await service.recall_context(_ctx("default.carol.default"), "x", plan=RecallPlan())


# ── the contract's invariants ────────────────────────────────────────────────


async def test_a_storage_failure_degrades_rather_than_raising(service) -> None:
    """The caller is assembling a reply: an exception costs the whole turn, a
    degraded result costs only the memories."""

    class _Broken(FakeMemoryBackend):
        async def search(self, *args, **kwargs):
            raise RuntimeError("store unreachable")

    service._router._runtimes[ALICE] = MemorySpaceRuntime(
        space_id=ALICE, backend=_Broken(), palace_path="/tmp/x"
    )

    result = await service.recall_context(_ctx(ALICE), "colour", plan=RecallPlan())

    assert result.degraded is True
    assert result.context == ""
    # Names what recall saw, which is a wrapped failure rather than the store's
    # own message — the original is in the logs with the space id. Asserting the
    # inner string here would be asserting how the exception propagates, and the
    # contract is only that an operator learns *what kind* of failure it was.
    assert result.degraded_reason
    assert "MemoryBackendUnavailable" in result.degraded_reason


async def test_search_degrades_the_same_way(service) -> None:
    class _Broken(FakeMemoryBackend):
        async def search(self, *args, **kwargs):
            raise RuntimeError("store unreachable")

    service._router._runtimes[ALICE] = MemorySpaceRuntime(
        space_id=ALICE, backend=_Broken(), palace_path="/tmp/x"
    )

    result = await service.search(_ctx(ALICE), "colour")

    assert result.degraded is True
    assert result.snippets == []


async def test_the_contract_result_carries_no_graph_or_turn_fields(service) -> None:
    """The narrowing that makes the contract worth having.

    Either field would let a caller work out whether this deployment keeps a
    graph, which is exactly the internal detail the contract withholds.
    """

    result = await service.recall_context(_ctx(ALICE), "x", plan=RecallPlan())

    emitted = result.model_dump()
    assert "kg_triples" not in emitted
    assert "working_memory" not in emitted


async def test_the_fused_method_is_the_only_place_the_extra_fields_come_from(
    service,
) -> None:
    """One producer for the transitional material, so it cannot become a second
    contract. When the client stops reading these, the method goes."""

    fused = await service.recall_fused(_ctx(ALICE), "x", plan=RecallPlan())

    assert "kg_triples" in fused
    assert "working_memory" in fused


# ── audience, which only means something once a space spans companions ───────


async def test_a_companions_private_memory_stays_with_that_companion(service) -> None:
    """The reason a space should be an owner rather than a companion.

    Both companions read one store here, so the audience column is what separates
    them. While a space *was* a companion they were separate stores and this
    distinction could not do anything at all.
    """

    backend = service._router._runtimes[ALICE].backend
    await backend.ingest_fragment(_fragment(ALICE, "owner likes green"))
    await backend.ingest_fragment(
        _fragment(
            ALICE,
            "our private joke",
            audience=companion_audience("comp_a"),
            room="nickname",
        )
    )

    for_a = await service.recall_context(
        _ctx(ALICE, companion="comp_a"), "green joke", plan=RecallPlan()
    )
    for_b = await service.recall_context(
        _ctx(ALICE, companion="comp_b"), "green joke", plan=RecallPlan()
    )

    assert "owner likes green" in for_a.context
    assert "owner likes green" in for_b.context, "the owner layer reaches every companion"
    assert "private joke" in for_a.context
    assert "private joke" not in for_b.context


async def test_held_spaces_reports_this_process_not_a_callers_memories(
    service,
) -> None:
    assert await service.held_spaces() == [ALICE, BOB]


# ── the fixed-space router ───────────────────────────────────────────────────


async def test_a_fixed_space_router_serves_its_one_space() -> None:
    """For a caller that already resolved its space and needs no pool."""

    from eidolon.memory.adapters.fixed_space_router import FixedSpaceRouter

    runtime = MemorySpaceRuntime(
        space_id=ALICE, backend=FakeMemoryBackend(), palace_path="/tmp/alice"
    )
    router = FixedSpaceRouter(runtime)

    assert router.serves(ALICE)
    assert await router.resolve(ALICE) is runtime
    assert router.held_spaces() == [ALICE]


async def test_a_fixed_space_router_refuses_any_other_space() -> None:
    """The property that keeps a routing bug from becoming a cross-tenant read.

    Answering every space id with the same handles would look like it was
    working, and would serve one owner's memories to another.
    """

    from eidolon.memory.adapters.fixed_space_router import FixedSpaceRouter

    router = FixedSpaceRouter(
        MemorySpaceRuntime(
            space_id=ALICE, backend=FakeMemoryBackend(), palace_path="/tmp/alice"
        )
    )

    assert not router.serves(BOB)
    with pytest.raises(UnknownMemorySpace):
        await router.resolve(BOB)


# ── the graph override ───────────────────────────────────────────────────────


async def test_a_caller_may_turn_the_graph_off_for_one_request(service) -> None:
    fused = await service.recall_fused(
        _ctx(ALICE), "x", plan=RecallPlan(), include_kg=False
    )

    assert fused["kg_triples"] == []


async def test_a_caller_cannot_turn_on_a_graph_this_deployment_lacks(service) -> None:
    """``include_kg=True`` against a deployment with no graph must not fail.

    The caller is asking for a graph to be consulted if there is one, not
    asserting that one exists — and a caller able to tell the difference would be
    able to discover whether this deployment keeps a graph.
    """

    fused = await service.recall_fused(
        _ctx(ALICE), "x", plan=RecallPlan(), include_kg=True
    )

    assert fused["degraded"] is False
    assert fused["kg_triples"] == []


# ── search is a lookup, recall is a recall ───────────────────────────────────


async def test_search_does_not_bring_in_the_graph_or_recent_turns(service) -> None:
    """Two different questions, and I collapsed them once already.

    ``search`` answers "what do you remember about this" — what is stored.
    ``recall_context`` answers "what is relevant to this turn", which is why it
    fuses the graph, recent turns and a theme channel and applies session
    filtering. Routing search through the recall path silently changed what an
    explicit user lookup returns.
    """

    backend = service._router._runtimes[ALICE].backend
    await backend.ingest_fragment(_fragment(ALICE, "likes the colour green"))

    found = await service.search(_ctx(ALICE), "colour")

    assert [s.text for s in found.snippets] == ["likes the colour green"]
    # The lookup shape has no room for either, which is the point.
    emitted = found.model_dump()
    assert "kg_triples" not in emitted
    assert "working_memory" not in emitted
    assert "context" not in emitted


async def test_search_and_recall_are_separate_result_types(service) -> None:
    """A caller cannot accidentally treat one as the other."""

    from eidolon_memory_contracts import RecallResult, SearchResult

    found = await service.search(_ctx(ALICE), "x")
    recalled = await service.recall_context(_ctx(ALICE), "x", plan=RecallPlan())

    assert isinstance(found, SearchResult)
    assert isinstance(recalled, RecallResult)
