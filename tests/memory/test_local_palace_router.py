"""One process, many spaces — with the isolation embedded storage requires.

The property that makes this worth doing is that a space's marginal cost is its
handles, not another copy of the embedding model. The property that makes it safe
is that spaces still cannot see each other, and each still has exactly one lock.
"""

from __future__ import annotations

import asyncio

import pytest

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRouter,
    MemorySpaceRuntime,
    MemorySpaceUnavailable,
    UnknownMemorySpace,
)


def _isolated_settings(root, *, kg_backend: str = "none") -> MemorySettings:
    """Settings confined to one temporary directory.

    ``run_dir`` matters as much as ``palaces_root``: that is where the per-space
    advisory locks live, and leaving it at its default would have tests taking
    locks in the developer's real run directory — and colliding with each other
    over spaces that merely share a name.
    """

    return MemorySettings.model_validate(
        {
            "runtime": {
                "palaces_root": str(root / "palaces"),
                "run_dir": str(root / "run"),
            },
            # Ranking is not what these tests are about, and the real embedder
            # would dominate their runtime.
            "mempalace": {"backend": "chroma", "offline_embedding": True},
            "kg": {"backend": kg_backend},
        }
    )


@pytest.fixture
def settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> MemorySettings:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    return _isolated_settings(tmp_path)


def test_it_satisfies_the_router_protocol(settings: MemorySettings) -> None:
    assert isinstance(LocalPalaceRouter(settings), MemorySpaceRouter)


async def test_construction_opens_nothing(settings: MemorySettings) -> None:
    """Startup cost should track spaces used, not spaces configured."""

    router = LocalPalaceRouter(settings)

    assert router.held_spaces() == []


async def test_resolving_the_same_space_twice_returns_the_same_handles(
    settings: MemorySettings,
) -> None:
    """Reopening storage per operation would defeat the point."""

    router = LocalPalaceRouter(settings)
    try:
        first = await router.resolve("alice")
        second = await router.resolve("alice")

        assert first is second
        assert router.held_spaces() == ["alice"]
    finally:
        await router.aclose()


async def test_one_process_serves_several_spaces(settings: MemorySettings) -> None:
    router = LocalPalaceRouter(settings)
    try:
        runtimes = [await router.resolve(name) for name in ("alice", "bob", "carol")]

        assert router.held_spaces() == ["alice", "bob", "carol"]
        assert len({runtime.palace_path for runtime in runtimes}) == 3
    finally:
        await router.aclose()


async def test_each_space_gets_its_own_lock(settings: MemorySettings) -> None:
    """A shared lock would let one space's slow write block another's read."""

    router = LocalPalaceRouter(settings)
    try:
        alice = await router.resolve("alice")
        bob = await router.resolve("bob")

        assert alice.backend.lock is not None
        assert alice.backend.lock is not bob.backend.lock
    finally:
        await router.aclose()


async def test_the_working_memory_ring_shares_its_space_lock(
    settings: MemorySettings,
) -> None:
    """One lock per space, so there is no ordering between two to get wrong."""

    router = LocalPalaceRouter(settings)
    try:
        runtime = await router.resolve("alice")

        assert runtime.backend.working_memory is not None
        assert runtime.backend.working_memory._lock is runtime.backend.lock
    finally:
        await router.aclose()


async def test_what_one_space_stores_is_invisible_to_another(
    settings: MemorySettings,
) -> None:
    """The isolation guarantee, exercised rather than assumed."""

    router = LocalPalaceRouter(settings)
    try:
        alice = await router.resolve("alice")
        bob = await router.resolve("bob")

        await alice.backend.ingest_text(
            wing="Wing_Life",
            room="colour",
            text="alice likes green",
            metadata={"memory_space_id": "alice"},
        )

        assert await alice.backend.get_all("alice", limit=10)
        assert await bob.backend.get_all("bob", limit=10) == []
    finally:
        await router.aclose()


async def test_concurrent_first_resolves_build_one_set_of_handles(
    settings: MemorySettings,
) -> None:
    """Two turns for a new space arriving together must not open it twice."""

    router = LocalPalaceRouter(settings)
    try:
        results = await asyncio.gather(*(router.resolve("alice") for _ in range(5)))

        assert len({id(runtime) for runtime in results}) == 1
        assert router.held_spaces() == ["alice"]
    finally:
        await router.aclose()


async def test_resolving_different_spaces_does_not_serialise(
    settings: MemorySettings,
) -> None:
    """The pool lock guards the pool, not the spaces in it."""

    router = LocalPalaceRouter(settings)
    try:
        runtimes = await asyncio.gather(
            *(router.resolve(name) for name in ("alice", "bob", "carol", "dave"))
        )

        assert len({runtime.space_id for runtime in runtimes}) == 4
    finally:
        await router.aclose()


async def test_a_shard_refuses_spaces_outside_it(settings: MemorySettings) -> None:
    """How a supervisor bounds the blast radius of one process crashing."""

    router = LocalPalaceRouter(settings, allowed_spaces=["alice", "bob"])
    try:
        assert router.serves("alice")
        assert not router.serves("carol")
        await router.resolve("alice")

        with pytest.raises(UnknownMemorySpace, match="does not serve"):
            await router.resolve("carol")
    finally:
        await router.aclose()


async def test_the_pool_is_bounded(settings: MemorySettings) -> None:
    """Better to refuse than to open an unbounded number of SQLite files."""

    router = LocalPalaceRouter(settings, max_spaces=2)
    try:
        await router.resolve("alice")
        await router.resolve("bob")

        with pytest.raises(MemorySpaceUnavailable, match="at most 2"):
            await router.resolve("carol")
    finally:
        await router.aclose()


async def test_closing_twice_is_harmless(settings: MemorySettings) -> None:
    router = LocalPalaceRouter(settings)
    await router.resolve("alice")

    await router.aclose()
    await router.aclose()

    assert router.held_spaces() == []


async def test_the_graph_follows_configuration(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)

    # Separate directories, and separate space names: two routers claiming one
    # space is a conflict by design, which the next test covers.
    off = LocalPalaceRouter(_isolated_settings(tmp_path / "off", kg_backend="none"))
    on = LocalPalaceRouter(_isolated_settings(tmp_path / "on", kg_backend="sqlite"))
    try:
        assert not (await off.resolve("without-graph")).has_kg
        assert (await on.resolve("with-graph")).has_kg
    finally:
        await off.aclose()
        await on.aclose()


async def test_a_space_already_held_is_refused(settings: MemorySettings) -> None:
    """Two owners of one palace would route its writes unpredictably.

    The claim also covers the space's durable JetStream consumers, so this has to
    fail fast rather than degrade — a second holder is not a slower service, it is
    a service writing to the wrong place.
    """

    first = LocalPalaceRouter(settings)
    second = LocalPalaceRouter(settings)
    try:
        await first.resolve("alice")

        with pytest.raises(MemorySpaceUnavailable, match="already owned"):
            await second.resolve("alice")
    finally:
        await second.aclose()
        await first.aclose()


async def test_releasing_a_space_lets_another_process_take_it(
    settings: MemorySettings,
) -> None:
    """Otherwise a restart could not reclaim its own spaces."""

    first = LocalPalaceRouter(settings)
    await first.resolve("alice")
    await first.aclose()

    second = LocalPalaceRouter(settings)
    try:
        assert (await second.resolve("alice")).space_id == "alice"
    finally:
        await second.aclose()


async def test_a_palace_is_initialised_before_it_is_read(
    settings: MemorySettings,
) -> None:
    """A space with nothing stored must read as empty, not raise.

    MemPalace refuses to open a collection that was never created, so opening a
    space has to include initialising it. Without that, the first read of a fresh
    space fails instead of returning nothing.
    """

    router = LocalPalaceRouter(settings)
    try:
        runtime = await router.resolve("fresh")

        assert await runtime.backend.get_all("fresh", limit=10) == []
    finally:
        await router.aclose()


def test_a_runtime_is_immutable() -> None:
    """Handles are resolved per operation; mutating one would surprise a caller."""

    runtime = MemorySpaceRuntime(space_id="alice", backend=object(), palace_path="/tmp/x")

    with pytest.raises(Exception):
        runtime.space_id = "bob"


def test_both_claims_are_taken_and_the_space_filename_is_pinned(
    settings: MemorySettings, tmp_path
) -> None:
    """Two locks per space, and the older name must not move.

    The space name is pinned because a live process holds a path built from this
    exact string. If a new version looked somewhere else, both would open the same
    palace — the corruption the claim exists to prevent — and any deployment short
    of a clean full stop would hit it. The better name is not worth that.

    The palace claim is the one that is actually correct: Chroma's constraint is
    per *directory*, and two space ids can name one directory. Both are asserted
    here because dropping either reopens a different hole.
    """

    from eidolon.memory.config.memory_settings import resolve_run_dir
    from eidolon.memory.infrastructure.nats.names import nats_safe_name

    router = LocalPalaceRouter(settings)
    palace = tmp_path / "alice-palace"
    try:
        router._acquire_space_lock("alice", palace)

        run_dir = resolve_run_dir(settings)
        by_space = run_dir / f"eidolon-memory-agent-{nats_safe_name('alice')}.lock"
        by_palace = (
            run_dir
            / f"eidolon-memory-palace-{nats_safe_name(str(palace.resolve()))}.lock"
        )
        assert by_space.is_file(), f"expected the space claim at {by_space}"
        assert by_palace.is_file(), f"expected the palace claim at {by_palace}"
    finally:
        for handles in router._locks.values():
            for handle in handles:
                handle.close()
        router._locks.clear()
