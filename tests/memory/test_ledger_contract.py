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

import asyncio
from datetime import UTC, datetime

import pytest

from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.ports import (
    CommandStatusStore,
    DlqStore,
    ExtractionDecisionStore,
    SyncLedgerPort,
)
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.command_status import CommandStatusLedger
from eidolon.memory.infrastructure.dlq import DlqLedger
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


# ── dead letters, both storages ──────────────────────────────────────────────


@pytest.fixture(params=["embedded", "shared"])
async def dlq(request: pytest.FixtureRequest, tmp_path, postgres_pool):
    if request.param == "embedded":
        return DlqLedger(tmp_path / "dlq.sqlite3", space_id=SPACE)

    from eidolon.memory.infrastructure.ledgers_postgres import PostgresDlqLedger

    ledger = PostgresDlqLedger(postgres_pool, space_id=SPACE)
    await ledger.ensure_schema()
    return ledger


async def test_dlq_satisfies_the_port(dlq) -> None:
    assert isinstance(dlq, DlqStore)


async def test_a_failed_turn_round_trips(dlq) -> None:
    added = await dlq.add(
        subject="eidolon.memory.turn.alice", payload=b"raw-turn", error="boom", deliveries=3
    )

    loaded = await dlq.get(added.entry_id)

    assert loaded is not None
    assert loaded.subject == "eidolon.memory.turn.alice"
    assert loaded.error == "boom"
    assert loaded.deliveries == 3
    assert loaded.state == "unresolved"


async def test_an_empty_payload_is_refused(dlq) -> None:
    """Nothing to replay means nothing worth keeping."""

    with pytest.raises(ValueError):
        await dlq.add(subject="s", payload=b"", error="e", deliveries=1)


async def test_claiming_twice_gives_the_second_caller_nothing(dlq) -> None:
    """The property that makes replay safe with more than one worker.

    Both storages implement it as one conditional UPDATE, so whichever caller's
    statement matches the row owns the claim and the other matches nothing.
    """

    entry = await dlq.add(subject="s", payload=b"p", error="e", deliveries=1)

    first = await dlq.claim_replay(entry.entry_id)
    second = await dlq.claim_replay(entry.entry_id)

    assert first is not None
    assert first.payload == b"p"
    assert second is None


async def test_a_replayed_entry_leaves_the_queue(dlq) -> None:
    entry = await dlq.add(subject="s", payload=b"p", error="e", deliveries=1)
    await dlq.claim_replay(entry.entry_id)

    done = await dlq.mark_replayed(entry.entry_id)

    assert done.state == "replayed"
    assert [record.entry_id for record in await dlq.list(state="unresolved")] == []


async def test_a_released_entry_can_be_claimed_again(dlq) -> None:
    entry = await dlq.add(subject="s", payload=b"p", error="e", deliveries=1)
    await dlq.claim_replay(entry.entry_id)

    released = await dlq.release_replay(entry.entry_id, error="still failing")

    assert released.state == "unresolved"
    assert released.error == "still failing"
    assert await dlq.claim_replay(entry.entry_id) is not None


async def test_finishing_an_unclaimed_entry_is_an_error(dlq) -> None:
    """Reporting a replay nobody claimed means the caller lost track of state."""

    entry = await dlq.add(subject="s", payload=b"p", error="e", deliveries=1)

    with pytest.raises(ValueError):
        await dlq.mark_replayed(entry.entry_id)


async def test_an_entry_being_replayed_cannot_be_resolved(dlq) -> None:
    """Resolving in-flight work would discard a result about to arrive."""

    entry = await dlq.add(subject="s", payload=b"p", error="e", deliveries=1)
    await dlq.claim_replay(entry.entry_id)

    with pytest.raises(ValueError):
        await dlq.resolve(entry.entry_id, note="giving up")


async def test_stats_count_by_state(dlq) -> None:
    kept = await dlq.add(subject="s", payload=b"p1", error="e", deliveries=1)
    await dlq.add(subject="s", payload=b"p22", error="e", deliveries=1)
    await dlq.claim_replay(kept.entry_id)

    stats = await dlq.stats()

    assert stats.total == 2
    assert stats.unresolved == 1
    assert stats.replaying == 1
    assert stats.payload_bytes == 5


async def test_another_spaces_failures_are_not_listed(postgres_pool) -> None:
    """A dead letter holds a whole conversation turn, so this is the strongest
    reason the shared tables need a space column."""

    from eidolon.memory.infrastructure.ledgers_postgres import PostgresDlqLedger

    alice = PostgresDlqLedger(postgres_pool, space_id="default.alice.default")
    bob = PostgresDlqLedger(postgres_pool, space_id="default.bob.default")
    await alice.ensure_schema()

    entry = await alice.add(subject="s", payload=b"alice-turn", error="e", deliveries=1)

    assert await bob.get(entry.entry_id) is None
    assert await bob.list() == []
    assert (await bob.stats()).total == 0


# ── command status, both storages ────────────────────────────────────────────


@pytest.fixture(params=["embedded", "shared"])
async def commands(request: pytest.FixtureRequest, tmp_path, postgres_pool):
    if request.param == "embedded":
        return CommandStatusLedger(tmp_path / "cmd.sqlite3", space_id=SPACE)

    from eidolon.memory.infrastructure.ledgers_postgres import (
        PostgresCommandStatusLedger,
    )

    ledger = PostgresCommandStatusLedger(postgres_pool, space_id=SPACE)
    await ledger.ensure_schema()
    return ledger


async def test_command_status_satisfies_the_port(commands) -> None:
    assert isinstance(commands, CommandStatusStore)


async def test_an_unknown_request_has_no_status(commands) -> None:
    assert await commands.get("never-issued") is None


async def test_a_command_progresses_to_applied(commands) -> None:
    await commands.record_accepted("r1", kind="kg_add_triple")
    await commands.record_applied("r1", kind="kg_add_triple", resource_id="triple-9")

    record = await commands.get("r1")

    assert record.status == "applied"
    assert record.resource_id == "triple-9"


async def test_applied_is_never_downgraded(commands) -> None:
    """The invariant the whole ledger exists for.

    A late retry notification arriving after success must not make a finished
    command look unfinished — this projection may lose a final status, but it may
    never make an unapplied command look successful, nor the reverse.
    """

    await commands.record_accepted("r1", kind="k")
    await commands.record_applied("r1", kind="k")

    await commands.record_retrying("r1", kind="k", error="late retry")
    await commands.record_failed("r1", kind="k", error="late failure")

    assert (await commands.get("r1")).status == "applied"


async def test_a_later_successful_replay_upgrades_failed(commands) -> None:
    """Failure is terminal for reporting, not for the truth: a redelivered
    command that then succeeds should say so."""

    await commands.record_failed("r1", kind="k", error="first attempt")

    await commands.record_applied("r1", kind="k")

    assert (await commands.get("r1")).status == "applied"


async def test_a_late_accepted_does_not_reset_progress(commands) -> None:
    """Redelivery re-announces acceptance; that must not rewind the record."""

    await commands.record_accepted("r1", kind="k")
    await commands.record_retrying("r1", kind="k", error="temporary")

    await commands.record_accepted("r1", kind="k")

    assert (await commands.get("r1")).status == "retrying"


async def test_attempts_count_everything_but_acceptance(commands) -> None:
    await commands.record_accepted("r1", kind="k")
    assert (await commands.get("r1")).attempts == 0

    await commands.record_retrying("r1", kind="k", error="e")
    await commands.record_retrying("r1", kind="k", error="e")

    assert (await commands.get("r1")).attempts == 2


async def test_a_blank_request_id_is_refused(commands) -> None:
    with pytest.raises(ValueError):
        await commands.record_accepted("  ", kind="k")


async def test_waiting_returns_immediately_once_terminal(commands) -> None:
    await commands.record_applied("r1", kind="k")

    record = await commands.wait_terminal("r1", timeout_seconds=5.0)

    assert record.status == "applied"


async def test_waiting_returns_the_latest_status_on_timeout(commands) -> None:
    """Not an exception: the caller asked how far a command got, and "still
    running" answers that."""

    await commands.record_accepted("r1", kind="k")

    record = await commands.wait_terminal("r1", timeout_seconds=0.05)

    assert record.status == "accepted"


async def test_waiting_sees_a_status_that_arrives_while_it_waits(commands) -> None:
    """The behaviour the two storages reach by different means — an in-process
    event locally, polling on shared storage."""

    await commands.record_accepted("r1", kind="k")

    async def _finish_shortly() -> None:
        await asyncio.sleep(0.05)
        await commands.record_applied("r1", kind="k")

    waiter = asyncio.create_task(commands.wait_terminal("r1", timeout_seconds=5.0))
    await _finish_shortly()

    assert (await waiter).status == "applied"


async def test_pruning_keeps_commands_still_in_flight(commands) -> None:
    """Removing an accepted row would make a running command unknowable."""

    await commands.record_accepted("running", kind="k")
    await commands.record_applied("done", kind="k")

    await commands.prune()

    assert (await commands.get("running")) is not None


async def test_another_spaces_command_is_not_visible(postgres_pool) -> None:
    from eidolon.memory.infrastructure.ledgers_postgres import (
        PostgresCommandStatusLedger,
    )

    alice = PostgresCommandStatusLedger(postgres_pool, space_id="default.alice.default")
    bob = PostgresCommandStatusLedger(postgres_pool, space_id="default.bob.default")
    await alice.ensure_schema()

    await alice.record_applied("shared-request-id", kind="k")

    assert await bob.get("shared-request-id") is None
    assert (await bob.stats()).total == 0


# ── where they differ on purpose ─────────────────────────────────────────────


async def test_the_shared_command_ledger_polls_rather_than_waiting_on_an_event(
    postgres_pool,
) -> None:
    """An in-process event cannot cross replicas.

    Locally MCP and the command worker share one ledger object, so an
    ``asyncio.Event`` is exact and free. With replicas the request may be served
    by one and the command applied by another, and an event set there is never
    seen here — so this implementation polls, and has no event machinery at all.
    """

    from eidolon.memory.infrastructure.ledgers_postgres import (
        PostgresCommandStatusLedger,
    )

    ledger = PostgresCommandStatusLedger(postgres_pool, space_id=SPACE)
    await ledger.ensure_schema()

    assert not hasattr(ledger, "_terminal_events")
    assert ledger.POLL_INTERVAL_SECONDS > 0


async def test_a_status_written_by_one_replica_is_seen_by_another(
    postgres_pool,
) -> None:
    """What the polling buys: the property the embedded ledger cannot have."""

    from eidolon.memory.infrastructure.ledgers_postgres import (
        PostgresCommandStatusLedger,
    )

    worker = PostgresCommandStatusLedger(postgres_pool, space_id=SPACE)
    await worker.ensure_schema()
    frontend = PostgresCommandStatusLedger(postgres_pool, space_id=SPACE)

    await worker.record_accepted("r1", kind="k")

    async def _apply_shortly() -> None:
        await asyncio.sleep(0.05)
        await worker.record_applied("r1", kind="k")

    waiter = asyncio.create_task(frontend.wait_terminal("r1", timeout_seconds=5.0))
    await _apply_shortly()

    assert (await waiter).status == "applied"


async def test_the_embedded_dlq_releases_claims_when_it_reopens(tmp_path) -> None:
    """Safe locally: a palace has one owning process, so a claim left behind can
    only be from this process dying."""

    path = tmp_path / "dlq.sqlite3"
    ledger = DlqLedger(path, space_id=SPACE)
    entry = await ledger.add(subject="s", payload=b"p", error="e", deliveries=1)
    await ledger.claim_replay(entry.entry_id)

    reopened = DlqLedger(path, space_id=SPACE)

    assert (await reopened.get(entry.entry_id)).state == "unresolved"


async def test_the_shared_dlq_does_not_release_claims_when_it_opens(
    postgres_pool,
) -> None:
    """The same reset would be destructive here.

    A replica starting up cannot tell another replica's in-flight entry from an
    abandoned one, so resetting on open would hand live work to a second worker.
    Stale claims are released by age instead, explicitly.
    """

    from eidolon.memory.infrastructure.ledgers_postgres import PostgresDlqLedger

    ledger = PostgresDlqLedger(postgres_pool, space_id=SPACE)
    await ledger.ensure_schema()
    entry = await ledger.add(subject="s", payload=b"p", error="e", deliveries=1)
    await ledger.claim_replay(entry.entry_id)

    another_replica = PostgresDlqLedger(postgres_pool, space_id=SPACE)
    await another_replica.ensure_schema()

    assert (await another_replica.get(entry.entry_id)).state == "replaying"

    # Age is the only signal available, so recovery is explicit and threshold-based.
    assert await another_replica.release_stale_claims(older_than_seconds=0) == 1
    assert (await another_replica.get(entry.entry_id)).state == "unresolved"


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
