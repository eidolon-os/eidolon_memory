"""What the router must guarantee.

The router is the abstraction that let one process serve many spaces: callers hand
it a space id and get that space's handles, and nothing above it knows how a space
is opened. These tests are that contract, stated so it survives changes to the
implementation behind it.

The guarantee that carries the most weight is ownership. Storage lives in files
inside each palace directory, so a palace must be held by exactly one process —
Chroma has no server-side concurrency control and the SQLite ledgers are
single-writer. A second claim is therefore refused, and that refusal is asserted
here rather than described in a comment.

There was a second router for shared storage, and this file used to run every test
against both to prove one codebase served two deployment shapes. That shape is
gone: this project is local. The parameterisation went with it, but the contract
did not — it is what a second implementation would have to satisfy.
"""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRouter,
    MemorySpaceUnavailable,
)


def _local_settings(root) -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "runtime": {
                "palaces_root": str(root / "palaces"),
                "run_dir": str(root / "run"),
            },
            "mempalace": {"backend": "chroma", "offline_embedding": True},
            "kg": {"backend": "none"},
        }
    )


@pytest.fixture
def router(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    yield LocalPalaceRouter(_local_settings(tmp_path))


# ── the contract ─────────────────────────────────────────────────────────────


def test_it_satisfies_the_router_protocol(router) -> None:
    assert isinstance(router, MemorySpaceRouter)


async def test_construction_opens_nothing(router) -> None:
    assert router.held_spaces() == []


async def test_resolving_twice_returns_the_same_handles(router) -> None:
    first = await router.resolve("alice")
    second = await router.resolve("alice")

    assert first is second
    await router.aclose()


async def test_a_runtime_knows_which_space_it_is_for(router) -> None:
    runtime = await router.resolve("alice")

    assert runtime.space_id == "alice"
    await router.aclose()


async def test_one_router_serves_several_spaces(router) -> None:
    await router.resolve("alice")
    await router.resolve("bob")

    assert sorted(router.held_spaces()) == ["alice", "bob"]
    await router.aclose()


async def test_spaces_do_not_share_a_lock(router) -> None:
    """One space's writes must not queue behind another's.

    Each palace is a separate set of files, so the serialisation each needs is
    its own. Sharing one lock across spaces would make a busy owner slow every
    other owner in the process.
    """

    alice = await router.resolve("alice")
    bob = await router.resolve("bob")

    assert alice.backend.lock is not bob.backend.lock
    await router.aclose()


async def test_what_one_space_stores_is_invisible_to_another(router) -> None:
    """The isolation guarantee, exercised rather than assumed."""

    alice = await router.resolve("alice")
    bob = await router.resolve("bob")

    await alice.backend.ingest_text(
        wing="Wing_Life",
        room="colour",
        text="alice likes green",
    )

    assert await bob.backend.search("colour", wing="Wing_Life") == []
    await router.aclose()


async def test_a_fresh_space_reads_empty_rather_than_raising(router) -> None:
    """Opening a space is several steps, and skipping the palace initialisation
    one makes the first read raise instead of returning nothing."""

    runtime = await router.resolve("newcomer")

    assert await runtime.backend.search("anything", wing="Wing_Life") == []
    await router.aclose()


async def test_closing_is_idempotent(router) -> None:
    await router.resolve("alice")

    await router.aclose()
    await router.aclose()

    assert router.held_spaces() == []


# ── ownership, which the storage forces ──────────────────────────────────────


async def test_a_palace_refuses_a_second_holder(tmp_path, monkeypatch) -> None:
    """A palace with two owners routes its writes unpredictably.

    This is why the process:space relation is 1:N and not N:N — a process may
    hold many palaces, but a palace is held by one process.
    """

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    settings = _local_settings(tmp_path)
    first, second = LocalPalaceRouter(settings), LocalPalaceRouter(settings)
    try:
        await first.resolve("alice")

        with pytest.raises(MemorySpaceUnavailable, match="already owned"):
            await second.resolve("alice")
    finally:
        await second.aclose()
        await first.aclose()


async def test_one_process_refuses_two_spaces_in_one_palace_directory(
    tmp_path, monkeypatch
) -> None:
    """The same rule, for the case the on-disk lock cannot see.

    That lock is keyed on the space id, which is right for "is another *process*
    serving this space" and wrong for this: the resource Chroma cannot share is the
    directory, and two space ids can name one directory.

    ``palace_path_override`` does exactly that — it applies to every space this
    router resolves. Without this guard a process holding two spaces under an
    override would compute one path twice, take two differently-named flocks
    because the names come from the space ids, and open one ``chroma.sqlite3``
    twice: the corruption the claim exists to prevent, arriving through the
    mechanism meant to prevent it. Unreachable while a process serves one space,
    reachable the moment it serves several.
    """

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    shared = tmp_path / "one-palace"
    router = LocalPalaceRouter(
        _local_settings(tmp_path), palace_path_override=str(shared)
    )
    try:
        await router.resolve("alice")

        with pytest.raises(MemorySpaceUnavailable, match="already serves"):
            await router.resolve("bob")

        assert router.held_spaces() == ["alice"]
    finally:
        await router.aclose()


async def test_two_routers_refuse_one_palace_under_different_space_ids(
    tmp_path, monkeypatch
) -> None:
    """The half an in-process registry cannot cover.

    Two routers stand in for two processes. They ask for *different* spaces, so the
    space-keyed claim is granted to both — and before the directory-keyed one
    existed, both then opened the same ``chroma.sqlite3``. Chroma's own constraint
    is that it "is not process-safe for concurrent writers sharing the same local
    persistence path", so the key has to be the path.
    """

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    settings = _local_settings(tmp_path)
    shared = str(tmp_path / "shared-palace")
    first = LocalPalaceRouter(settings, palace_path_override=shared)
    second = LocalPalaceRouter(settings, palace_path_override=shared)
    try:
        await first.resolve("alice")

        with pytest.raises(MemorySpaceUnavailable, match="palace directory"):
            await second.resolve("bob")
    finally:
        await second.aclose()
        await first.aclose()


async def test_a_shard_refuses_the_spaces_it_was_not_given(tmp_path, monkeypatch) -> None:
    """How a supervisor bounds what one crashing process takes down."""

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    router = LocalPalaceRouter(_local_settings(tmp_path), allowed_spaces=["alice"])
    try:
        assert router.serves("alice")
        assert not router.serves("bob")
    finally:
        await router.aclose()


def test_the_factory_builds_the_router_for_this_deployment(tmp_path, monkeypatch) -> None:
    """Callers ask for a router, not for a class.

    The indirection is what makes the space a parameter instead of the process's
    identity, and it is the seam a different storage shape would plug into.
    """

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)

    from eidolon.memory.adapters.space_routing import build_space_router

    built = build_space_router(_local_settings(tmp_path))

    assert isinstance(built, MemorySpaceRouter)
    assert isinstance(built, LocalPalaceRouter)


def test_the_shipped_example_is_a_valid_deployment() -> None:
    """The template has to load and produce a router, or it is documentation."""

    import pathlib

    import yaml

    from eidolon.memory.adapters.space_routing import build_space_router

    config = pathlib.Path(__file__).resolve().parents[2] / "config"
    settings = MemorySettings.model_validate(
        yaml.safe_load((config / "settings.example.yaml").read_text(encoding="utf-8"))
    )

    assert isinstance(build_space_router(settings), LocalPalaceRouter)
