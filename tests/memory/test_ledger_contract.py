"""One suite, both storages — the switch is proven, not designed.

Local and cloud are meant to be the same service with different storage
underneath. That claim is only worth something if the same behavioural tests pass
against both, so every test here runs twice: once on the SQLite ledger inside a
palace, once on the PostgreSQL ledger a replica reaches over the network.

Where the two genuinely differ, the difference is asserted rather than smoothed
over. A test that passed against both by avoiding what separates them would be
the most misleading kind of green.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.ports import ExtractionDecisionStore, SyncLedgerPort
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger
from eidolon.memory.infrastructure.sync_ledger import SyncLedger

SPACE = "default.alice.default"
VERSION = "rules:v2"


def _record(
    *,
    turn_id: str = "turn-1",
    input_hash: str = "hash-1",
    space: str = SPACE,
    version: str = VERSION,
) -> ExtractionDecisionRecord:
    return ExtractionDecisionRecord(
        memory_space_id=space,
        source_turn_id=turn_id,
        extractor_version=version,
        input_hash=input_hash,
        decision=StewardDecision(should_write=True, reason="test"),
        intents=[],
        created_at=datetime.now(UTC).isoformat(),
    )


# ── the two storages, behind one fixture ─────────────────────────────────────


@pytest.fixture(params=["embedded", "shared"])
async def decisions(request: pytest.FixtureRequest, tmp_path, postgres_pool):
    """The decision ledger, once per storage shape.

    ``postgres_pool`` is requested for both cases rather than looked up inside
    the shared branch: an async fixture cannot pull another async fixture through
    ``getfixturevalue``. The server is session-scoped, so the embedded case pays
    nothing beyond a schema it does not use.

    The shared case is never skipped for a missing server — a skip there would
    mean the cloud half of a switchability claim silently stops being checked.
    """

    if request.param == "embedded":
        return ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")

    from eidolon.memory.infrastructure.ledgers_postgres import (
        PostgresExtractionDecisionLedger,
    )

    ledger = PostgresExtractionDecisionLedger(postgres_pool)
    await ledger.ensure_schema()
    return ledger


# ── behaviour that must be identical ─────────────────────────────────────────


async def test_it_satisfies_the_port(decisions) -> None:
    """Whichever storage it is, it is handed over as the same protocol."""

    assert isinstance(decisions, ExtractionDecisionStore)


async def test_an_absent_decision_reads_as_none(decisions) -> None:
    assert await decisions.get(SPACE, "never-stored", VERSION) is None


async def test_a_decision_round_trips(decisions) -> None:
    stored = await decisions.put_if_absent(_record())

    loaded = await decisions.get(SPACE, "turn-1", VERSION)

    assert loaded is not None
    assert loaded.input_hash == stored.input_hash
    assert loaded.memory_space_id == SPACE
    assert loaded.extractor_version == VERSION


async def test_reprocessing_a_turn_returns_the_stored_decision(decisions) -> None:
    """The property the ledger exists for.

    A turn redelivered by the bus must not be extracted a second time; the first
    conclusion is the durable one.
    """

    first = await decisions.put_if_absent(_record(input_hash="hash-1"))
    again = await decisions.put_if_absent(_record(input_hash="hash-1"))

    assert again.input_hash == first.input_hash


async def test_the_same_identity_with_different_input_is_a_conflict(decisions) -> None:
    """Two different turns cannot claim one identity.

    Silently keeping the first would hide that something upstream reused a turn
    id, and silently overwriting would lose a decision already projected.
    """

    await decisions.put_if_absent(_record(input_hash="hash-1"))

    with pytest.raises(ExtractionDecisionConflict):
        await decisions.put_if_absent(_record(input_hash="hash-2"))


async def test_a_different_extractor_version_is_a_separate_decision(decisions) -> None:
    """Version is part of the identity, so an upgraded steward re-decides."""

    await decisions.put_if_absent(_record(version="rules:v2"))
    await decisions.put_if_absent(_record(version="rules:v3", input_hash="hash-9"))

    assert (await decisions.get(SPACE, "turn-1", "rules:v2")).input_hash == "hash-1"
    assert (await decisions.get(SPACE, "turn-1", "rules:v3")).input_hash == "hash-9"


async def test_two_spaces_cannot_see_each_others_decisions(decisions) -> None:
    """On shared storage both rows live in one table, so this is the isolation
    boundary rather than a file-system one."""

    await decisions.put_if_absent(_record(space="default.alice.default"))
    await decisions.put_if_absent(
        _record(space="default.bob.default", input_hash="bob-hash")
    )

    alice = await decisions.get("default.alice.default", "turn-1", VERSION)
    bob = await decisions.get("default.bob.default", "turn-1", VERSION)

    assert alice.input_hash == "hash-1"
    assert bob.input_hash == "bob-hash"


# ── where they differ on purpose ─────────────────────────────────────────────


# ── device sync, both storages ───────────────────────────────────────────────


@pytest.fixture(params=["embedded", "shared"])
async def sync(request: pytest.FixtureRequest, tmp_path, postgres_pool):
    """The sync ledger for space ``SPACE``, once per storage shape."""

    if request.param == "embedded":
        return SyncLedger(tmp_path / "sync.sqlite3", space_id=SPACE)

    from eidolon.memory.infrastructure.ledgers_postgres import PostgresSyncLedger

    ledger = PostgresSyncLedger(postgres_pool, space_id=SPACE)
    await ledger.ensure_schema()
    return ledger


async def test_sync_satisfies_the_port(sync) -> None:
    assert isinstance(sync, SyncLedgerPort)


async def test_an_unseen_batch_is_not_seen(sync) -> None:
    assert await sync.seen(event_id="e1", idempotency_hash="h1") is False


async def test_a_marked_batch_is_seen(sync) -> None:
    await sync.mark_synced(
        event_id="e1",
        device_id="d1",
        instance_id="i1",
        turn_id="t1",
        idempotency_hash="h1",
    )

    assert await sync.seen(event_id="e1", idempotency_hash="h1") is True


async def test_the_same_payload_under_a_new_event_id_is_still_seen(sync) -> None:
    """A device that retries with a fresh event id must not replay the turn.

    The hash is what identifies the work; the event id only identifies the
    attempt.
    """

    await sync.mark_synced(
        event_id="e1",
        device_id="d1",
        instance_id="i1",
        turn_id="t1",
        idempotency_hash="h1",
    )

    assert await sync.seen(event_id="e2", idempotency_hash="h1") is True


async def test_marking_the_same_batch_twice_is_not_an_error(sync) -> None:
    """Reached by a caller that raced ``seen``. The record it wanted exists,
    which is the outcome rather than a failure."""

    for _ in range(2):
        await sync.mark_synced(
            event_id="e1",
            device_id="d1",
            instance_id="i1",
            turn_id="t1",
            idempotency_hash="h1",
        )

    assert await sync.seen(event_id="e1", idempotency_hash="h1") is True


async def test_another_spaces_batch_does_not_count_as_seen(postgres_pool) -> None:
    """The failure the space column exists to prevent.

    Two spaces on shared storage are in one table. Scoped by event id alone —
    which a per-palace file gave for free — one owner's sync history would make
    another owner's turns look already-applied and silently drop them.
    """

    from eidolon.memory.infrastructure.ledgers_postgres import PostgresSyncLedger

    alice = PostgresSyncLedger(postgres_pool, space_id="default.alice.default")
    bob = PostgresSyncLedger(postgres_pool, space_id="default.bob.default")
    await alice.ensure_schema()

    await alice.mark_synced(
        event_id="shared-id",
        device_id="d1",
        instance_id="i1",
        turn_id="t1",
        idempotency_hash="shared-hash",
    )

    assert await alice.seen(event_id="shared-id", idempotency_hash="shared-hash") is True
    assert await bob.seen(event_id="shared-id", idempotency_hash="shared-hash") is False


# ── where they differ on purpose ─────────────────────────────────────────────


async def test_the_shared_ledger_holds_no_lock(postgres_pool) -> None:
    """A lock held across a round trip would serialise the concurrency this
    deployment exists to have.

    The embedded ledger depends on a single owning process instead; correctness
    here comes from the primary key and the transaction.
    """

    from eidolon.memory.infrastructure.ledgers_postgres import (
        PostgresExtractionDecisionLedger,
    )

    ledger = PostgresExtractionDecisionLedger(postgres_pool)

    assert getattr(ledger, "lock", None) is None
