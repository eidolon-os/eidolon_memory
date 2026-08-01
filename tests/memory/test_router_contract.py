"""What every router must guarantee, and what only the shared-store one may.

The two routers exist so that one codebase serves both deployment shapes. That
only holds if they agree on a contract — and if the ways they differ are the ways
we intended, rather than whatever each happened to implement.

The difference that matters is ownership. Embedded storage must have exactly one
process holding a palace, so a second claim is refused. Shared storage must let any
replica serve any space, so a second claim is normal. That asymmetry is the whole
of horizontal scalability, and it is asserted here rather than described in a
comment.
"""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
from eidolon.memory.adapters.shared_store_router import SharedStoreRouter
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


def _shared_settings() -> MemorySettings:
    """Milvus Lite against a file: a remote store's shape without a server.

    Enough to exercise routing behaviour offline. The real server path is covered
    by tests/memory/test_live_milvus.py.
    """

    return MemorySettings.model_validate(
        {
            "mempalace": {"backend": "milvus", "offline_embedding": True},
            "kg": {"backend": "none"},
        }
    )


@pytest.fixture(params=["local", "shared"])
def router(request, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)

    if request.param == "local":
        made = LocalPalaceRouter(_local_settings(tmp_path))
    else:
        made = SharedStoreRouter(_shared_settings(), ephemeral_root=tmp_path / "ephemeral")
    yield made


# ── what both must do ────────────────────────────────────────────────────────


def test_it_satisfies_the_router_protocol(router) -> None:
    assert isinstance(router, MemorySpaceRouter)


async def test_construction_opens_nothing(router) -> None:
    assert router.held_spaces() == []


async def test_resolving_twice_returns_the_same_handles(router) -> None:
    first = await router.resolve("alice")
    second = await router.resolve("alice")

    assert first is second


async def test_a_runtime_knows_which_space_it_is_for(router) -> None:
    """Callers pass the id back down on every store call; it must be right."""

    assert (await router.resolve("alice")).space_id == "alice"


async def test_one_router_serves_several_spaces(router) -> None:
    for name in ("alice", "bob", "carol"):
        await router.resolve(name)

    assert router.held_spaces() == ["alice", "bob", "carol"]


async def test_spaces_do_not_share_a_lock(router) -> None:
    """One space's slow write must not block another's read."""

    alice = await router.resolve("alice")
    bob = await router.resolve("bob")

    assert alice.backend.lock is not bob.backend.lock


async def test_what_one_space_stores_is_invisible_to_another(router) -> None:
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


async def test_a_fresh_space_reads_empty_rather_than_raising(router) -> None:
    runtime = await router.resolve("fresh")

    assert await runtime.backend.get_all("fresh", limit=10) == []


async def test_closing_is_idempotent(router) -> None:
    await router.resolve("alice")

    await router.aclose()
    await router.aclose()

    assert router.held_spaces() == []


# ── where they must differ ───────────────────────────────────────────────────


async def test_embedded_storage_refuses_a_second_holder(tmp_path, monkeypatch) -> None:
    """A palace with two owners routes its writes unpredictably."""

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


async def test_shared_storage_lets_two_replicas_serve_one_space(tmp_path) -> None:
    """This is horizontal scalability, stated as a test.

    Two routers for the same space, concurrently, with no coordination between
    them — which is what a load balancer in front of N replicas produces. If this
    ever raises, replicas have stopped being interchangeable.
    """

    settings = _shared_settings()
    replica_a = SharedStoreRouter(settings, ephemeral_root=tmp_path / "a")
    replica_b = SharedStoreRouter(settings, ephemeral_root=tmp_path / "b")
    try:
        a = await replica_a.resolve("alice")
        b = await replica_b.resolve("alice")

        assert a.space_id == b.space_id == "alice"
        assert a.palace_path != b.palace_path, "each replica keeps its own bookkeeping"
    finally:
        await replica_b.aclose()
        await replica_a.aclose()


async def test_a_replica_holds_no_claim_to_release(tmp_path) -> None:
    """Closing a replica must not be a step another replica waits on."""

    settings = _shared_settings()
    replica = SharedStoreRouter(settings, ephemeral_root=tmp_path / "a")
    await replica.resolve("alice")
    await replica.aclose()

    # Immediately, with no handoff.
    other = SharedStoreRouter(settings, ephemeral_root=tmp_path / "b")
    try:
        assert (await other.resolve("alice")).space_id == "alice"
    finally:
        await other.aclose()


async def test_a_replica_keeps_no_turn_ring(tmp_path) -> None:
    """Process memory would make recall depend on which replica answered."""

    router = SharedStoreRouter(_shared_settings(), ephemeral_root=tmp_path / "a")
    try:
        runtime = await router.resolve("alice")
        snapshot = await runtime.backend.working_memory.snapshot()

        assert snapshot == []
    finally:
        await router.aclose()


async def test_a_shard_of_embedded_spaces_refuses_the_rest(tmp_path, monkeypatch) -> None:
    """How a supervisor bounds what one crashing process takes down."""

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    router = LocalPalaceRouter(_local_settings(tmp_path), allowed_spaces=["alice"])
    try:
        assert router.serves("alice")
        assert not router.serves("bob")
    finally:
        await router.aclose()


async def test_shared_storage_shards_nothing(tmp_path) -> None:
    """Any replica, any space — otherwise a balancer would need affinity."""

    router = SharedStoreRouter(_shared_settings(), ephemeral_root=tmp_path / "a")

    assert router.serves("alice")
    assert router.serves("anything-else")
    assert not router.serves("   ")


# ── the deployment shape follows from storage, not from a mode flag ───────────


def test_the_router_follows_the_storage_configuration(tmp_path, monkeypatch) -> None:
    """No cloud switch: which router runs is implied by where data lives.

    A separate mode flag could disagree with the storage config — claiming cloud
    while keeping data in a file — so there is only one source of truth.
    """

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)

    from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
    from eidolon.memory.adapters.shared_store_router import SharedStoreRouter
    from eidolon.memory.adapters.space_routing import build_space_router, storage_is_embedded

    local = build_space_router(_local_settings(tmp_path))
    shared = build_space_router(_shared_settings(), ephemeral_root=tmp_path / "e")

    assert isinstance(local, LocalPalaceRouter)
    assert isinstance(shared, SharedStoreRouter)
    assert storage_is_embedded(_local_settings(tmp_path))
    assert not storage_is_embedded(_shared_settings())


def test_sharding_is_ignored_where_it_has_no_meaning(tmp_path) -> None:
    """On shared storage every replica serves everything.

    Half-honouring a shard here would describe an affinity the architecture does
    not have, and a balancer would then have to know about it.
    """

    from eidolon.memory.adapters.space_routing import build_space_router

    shared = build_space_router(
        _shared_settings(), allowed_spaces=["alice"], ephemeral_root=tmp_path / "e"
    )

    assert shared.serves("bob")


def test_the_deployment_profiles_choose_different_routers() -> None:
    """The shipped examples must actually produce the two shapes."""

    import pathlib

    import yaml

    from eidolon.memory.adapters.space_routing import storage_is_embedded

    config = pathlib.Path(__file__).resolve().parents[2] / "config"

    def _load(name: str) -> MemorySettings:
        return MemorySettings.model_validate(
            yaml.safe_load((config / name).read_text(encoding="utf-8"))
        )

    assert storage_is_embedded(_load("settings.example.yaml"))
    assert not storage_is_embedded(_load("settings.cloud.example.yaml"))


# ── the ledgers a router hands over ──────────────────────────────────────────


async def test_the_shared_router_opens_ledgers_in_the_database(
    tmp_path, postgres_dsn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured for shared ledgers, a replica must actually get working ones.

    The router previously returned an empty SpaceLedgers() and every consumer's
    None handling kept the service running, so the absence looked like a working
    deployment right up until a commitment query came back empty.
    """

    monkeypatch.setenv("EIDOLON_MEMORY_LEDGER_PG_DSN", postgres_dsn)
    settings = MemorySettings.model_validate(
        {
            "mempalace": {"backend": "milvus", "offline_embedding": True},
            "kg": {"backend": "none"},
            "ledgers": {"backend": "postgres"},
        }
    )
    router = SharedStoreRouter(settings, ephemeral_root=tmp_path / "ephemeral")

    try:
        runtime = await router.resolve("default.alice.default")

        assert runtime.ledgers.decisions is not None
        assert runtime.ledgers.sync is not None
        assert runtime.ledgers.dlq is not None
        assert runtime.ledgers.command_status is not None
        # Reachable, not merely constructed.
        assert (
            await runtime.ledgers.sync.seen(event_id="e1", idempotency_hash="h1")
        ) is False
    finally:
        await router.aclose()


async def test_shared_storage_without_ledger_config_serves_without_them(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misconfiguration degrades rather than refusing to start.

    Palace-backed ledgers on a replica would be per-replica state, which is what
    this router exists not to have. Vector recall alone is still a working
    service, and failing startup would take a deployment down over a setting that
    can be corrected while it runs.
    """

    monkeypatch.delenv("EIDOLON_MEMORY_LEDGER_PG_DSN", raising=False)
    router = SharedStoreRouter(_shared_settings(), ephemeral_root=tmp_path / "ephemeral")

    try:
        runtime = await router.resolve("default.alice.default")

        assert runtime.ledgers.decisions is None
        assert runtime.backend is not None
    finally:
        await router.aclose()
