"""What the six ledgers must do, stated against their port rather than their file.

Each test takes a ledger through a fixture and asserts on the behaviour the port
promises — that a replayed turn is recognised, that a claim is not handed to two
workers, that an applied command is never downgraded. None of them reach into
SQLite.

That is deliberate and it is what makes them a contract rather than
implementation tests. This suite once ran every case twice, against the SQLite
ledgers and against PostgreSQL ones, to prove local and cloud were the same
service with different storage. The PostgreSQL half is gone with the cloud shape;
the suite kept the property that mattered — the behaviour is specified
independently of where the rows live.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from eidolon.memory.domain.commitment import CommitmentConflict
from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
)
from eidolon.memory.domain.ports import (
    CommandStatusStore,
    CommitmentStore,
    DlqStore,
    ExtractionDecisionStore,
    SyncLedgerPort,
)
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.command_status import CommandStatusLedger
from eidolon.memory.infrastructure.commitments import CommitmentLedger
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


# ── the ledgers, each behind a fixture ───────────────────────────────────────


@pytest.fixture
async def decisions(tmp_path):
    """The extraction-decision ledger."""

    return ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")


# ── extraction decisions ─────────────────────────────────────────────────────


async def test_it_satisfies_the_port(decisions) -> None:
    """Handed to callers as the protocol, not as the class."""

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
    """The ledger filters by space rather than relying on its file.

    Each space has its own file today, so a query that forgot the space column
    would still pass by accident. Asserted anyway: the column is what the
    isolation actually rests on, and three ledgers were once missing it."""

    await decisions.put_if_absent(_record(space="default.alice.default"))
    await decisions.put_if_absent(
        _record(space="default.bob.default", input_hash="bob-hash")
    )

    alice = await decisions.get("default.alice.default", "turn-1", VERSION)
    bob = await decisions.get("default.bob.default", "turn-1", VERSION)

    assert alice.input_hash == "hash-1"
    assert bob.input_hash == "bob-hash"


# ── device sync ──────────────────────────────────────────────────────────────


@pytest.fixture
async def sync(tmp_path):
    """The sync ledger for space ``SPACE``."""

    return SyncLedger(tmp_path / "sync.sqlite3", space_id=SPACE)


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


@pytest.fixture
async def dlq(tmp_path):
    return DlqLedger(tmp_path / "dlq.sqlite3", space_id=SPACE)


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


@pytest.fixture
async def commands(tmp_path):
    return CommandStatusLedger(tmp_path / "cmd.sqlite3", space_id=SPACE)


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


async def test_the_embedded_dlq_releases_claims_when_it_reopens(tmp_path) -> None:
    """Safe locally: a palace has one owning process, so a claim left behind can
    only be from this process dying."""

    path = tmp_path / "dlq.sqlite3"
    ledger = DlqLedger(path, space_id=SPACE)
    entry = await ledger.add(subject="s", payload=b"p", error="e", deliveries=1)
    await ledger.claim_replay(entry.entry_id)

    reopened = DlqLedger(path, space_id=SPACE)

    assert (await reopened.get(entry.entry_id)).state == "unresolved"


def test_an_empty_outdated_ledger_is_rebuilt(tmp_path) -> None:
    """Nothing is lost, and failing would block a deployment over an empty file."""

    import sqlite3

    path = tmp_path / "dlq.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE dlq_entries (entry_id TEXT PRIMARY KEY, subject TEXT, "
            "payload BLOB, error TEXT, deliveries INTEGER, state TEXT, "
            "replay_attempts INTEGER, resolution_note TEXT, created_at TEXT, "
            "updated_at TEXT)"
        )

    DlqLedger(path, space_id=SPACE)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dlq_entries)")}
    assert "memory_space_id" in columns


def test_a_populated_outdated_ledger_refuses_to_open(tmp_path) -> None:
    """The operator's call, not ours.

    Dead letters are failed turns worth inspecting and sync events are what stop
    a device replaying itself. Dropping either silently would be destroying data
    to avoid an error message — so this raises, naming the file.
    """

    import sqlite3

    from eidolon.memory.infrastructure.ledger_sql import LedgerSchemaOutdated

    path = tmp_path / "dlq.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE dlq_entries (entry_id TEXT PRIMARY KEY, subject TEXT, "
            "payload BLOB, error TEXT, deliveries INTEGER, state TEXT, "
            "replay_attempts INTEGER, resolution_note TEXT, created_at TEXT, "
            "updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO dlq_entries VALUES ('e1', 's', X'00', 'err', 1, "
            "'unresolved', 0, NULL, 'now', 'now')"
        )

    with pytest.raises(LedgerSchemaOutdated) as raised:
        DlqLedger(path, space_id=SPACE)

    assert "dlq.sqlite3" in str(raised.value)
    assert "1 row" in str(raised.value)


async def test_a_current_ledger_keeps_its_rows_when_reopened(tmp_path) -> None:
    """The guard must not disturb a file this version wrote.

    A check that dropped a current table would look identical to one that
    rebuilt an outdated one, so this asserts the row survives rather than just
    that opening succeeds.
    """

    path = tmp_path / "dlq.sqlite3"
    first = DlqLedger(path, space_id=SPACE)
    entry = await first.add(subject="s", payload=b"p", error="e", deliveries=1)

    reopened = DlqLedger(path, space_id=SPACE)

    assert await reopened.get(entry.entry_id) is not None


async def test_a_populated_command_status_is_rebuilt_rather_than_refused(
    tmp_path,
) -> None:
    """The one table whose rows are expendable by design.

    It is a projection of the command stream: a lost final status shows a command
    as accepted again, never an unapplied one as successful. Refusing to start
    over rows like that would be strictness with no safety behind it — and it
    would have kept four live agents from starting.
    """

    import sqlite3

    path = tmp_path / "cmd.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE command_status (request_id TEXT PRIMARY KEY, kind TEXT, "
            "status TEXT, resource_id TEXT, error TEXT, attempts INTEGER, "
            "created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO command_status VALUES ('r1', 'k', 'applied', NULL, NULL, "
            "1, 'now', 'now')"
        )

    ledger = CommandStatusLedger(path, space_id=SPACE)

    assert await ledger.get("r1") is None  # rebuilt, so the old row is gone
    await ledger.record_accepted("r2", kind="k")
    assert (await ledger.get("r2")).status == "accepted"


# ── commitments ──────────────────────────────────────────────────────────────


def _commitment_intent(
    intent_id: str,
    *,
    operation: str = "add",
    status: str | None = None,
    target_id: str | None = None,
    participants: list[str] | None = None,
    action: str = "take them to the dinosaur park",
    due_at: str | None = None,
    space: str = SPACE,
):
    from eidolon_memory_contracts import MemoryIntent

    attributes = {
        "beneficiaries": ["companion:default"],
        "participants": participants or [],
        "condition": "once there is a body",
    }
    if status is not None:
        attributes["status"] = status
    if due_at is not None:
        attributes["due_at"] = due_at
    return MemoryIntent(
        intent_id=intent_id,
        memory_space_id=space,
        source_event_id=f"turn:{intent_id}",
        authority="explicit_user",
        intent_type="commitment",
        raw_claim="I'll take you to the dinosaur park some day",
        operation_hint=operation,
        target_id=target_id,
        subject="self",
        predicate="promised",
        object=action,
        confidence=1.0,
        attributes=attributes,
    )


@pytest.fixture
async def commitments(tmp_path):
    return CommitmentLedger(tmp_path / "commitments.sqlite3")


async def test_commitments_satisfies_the_port(commitments) -> None:
    assert isinstance(commitments, CommitmentStore)


async def test_a_promise_round_trips(commitments) -> None:
    result = await commitments.apply(_commitment_intent("i1"))

    assert result.commitment_created is True
    assert result.commitment.status == "proposed"
    stored = await commitments.get(SPACE, result.commitment.commitment_id)
    assert stored.action == "take them to the dinosaur park"


async def test_an_explicit_confirmation_skips_proposed(commitments) -> None:
    """The user already said it; recording it as merely proposed understates that."""

    result = await commitments.apply(_commitment_intent("i1", operation="confirm"))

    assert result.commitment.status == "confirmed"


async def test_replaying_an_intent_returns_the_stored_decision(commitments) -> None:
    """The property the revision table exists for."""

    first = await commitments.apply(_commitment_intent("i1"))
    again = await commitments.apply(_commitment_intent("i1"))

    assert again.revision_created is False
    assert again.commitment_created is False
    assert again.commitment.revision == first.commitment.revision


async def test_the_same_intent_id_with_a_different_payload_is_a_conflict(
    commitments,
) -> None:
    await commitments.apply(_commitment_intent("i1"))

    with pytest.raises(CommitmentConflict):
        await commitments.apply(_commitment_intent("i1", action="something else"))


async def test_participants_accumulate_across_revisions(commitments) -> None:
    """Someone named earlier is still involved when a later turn omits them."""

    first = await commitments.apply(_commitment_intent("i1", participants=["mum"]))
    await commitments.apply(
        _commitment_intent(
            "i2",
            operation="update",
            target_id=first.commitment.commitment_id,
            participants=["dad"],
        )
    )

    stored = await commitments.get(SPACE, first.commitment.commitment_id)
    assert set(stored.participants) == {"mum", "dad"}
    assert stored.revision == 2


async def test_a_terminal_promise_cannot_reopen(commitments) -> None:
    """A fulfilled promise does not become proposed because a turn mentioned it."""

    first = await commitments.apply(_commitment_intent("i1", operation="confirm"))
    await commitments.apply(
        _commitment_intent(
            "i2",
            operation="update",
            status="fulfilled",
            target_id=first.commitment.commitment_id,
        )
    )

    with pytest.raises(CommitmentConflict):
        await commitments.apply(
            _commitment_intent(
                "i3",
                operation="update",
                status="proposed",
                target_id=first.commitment.commitment_id,
            )
        )


async def test_identity_fields_cannot_be_changed(commitments) -> None:
    first = await commitments.apply(_commitment_intent("i1"))

    with pytest.raises(CommitmentConflict):
        await commitments.apply(
            _commitment_intent(
                "i2",
                operation="update",
                target_id=first.commitment.commitment_id,
                action="a completely different promise",
            )
        )


async def test_targeting_a_commitment_that_does_not_exist_is_a_conflict(
    commitments,
) -> None:
    with pytest.raises(CommitmentConflict):
        await commitments.apply(
            _commitment_intent("i1", operation="update", target_id="commitment:nope")
        )


async def test_only_active_promises_are_listed(commitments) -> None:
    active = await commitments.apply(_commitment_intent("i1"))
    done = await commitments.apply(_commitment_intent("i2", action="water the plants"))
    await commitments.apply(
        _commitment_intent(
            "i3",
            operation="update",
            status="cancelled",
            target_id=done.commitment.commitment_id,
            action="water the plants",
        )
    )

    page = await commitments.list_current_page(SPACE, include_terminal=False)

    assert [c.commitment_id for c in page.commitments] == [active.commitment.commitment_id]


async def test_the_page_orders_by_due_date_soonest_first(commitments) -> None:
    """Pins the ordering that replaced SQLite's julianday().

    due_at is ISO 8601, whose lexicographic order is its chronological order, so
    the shared statement can sort on the text. Undated promises come last —
    otherwise NULL sorting would decide priority differently per database.
    """

    await commitments.apply(
        _commitment_intent("late", action="later thing", due_at="2027-01-01T00:00:00Z")
    )
    await commitments.apply(
        _commitment_intent("soon", action="sooner thing", due_at="2026-01-01T00:00:00Z")
    )
    await commitments.apply(_commitment_intent("undated", action="someday thing"))

    page = await commitments.list_current_page(SPACE, include_terminal=False)

    assert [c.action for c in page.commitments] == [
        "sooner thing",
        "later thing",
        "someday thing",
    ]


async def test_history_is_returned_oldest_first(commitments) -> None:
    first = await commitments.apply(_commitment_intent("i1"))
    await commitments.apply(
        _commitment_intent(
            "i2",
            operation="update",
            status="confirmed",
            target_id=first.commitment.commitment_id,
        )
    )

    history = await commitments.history(SPACE, first.commitment.commitment_id)

    assert [r.status for r in history] == ["proposed", "confirmed"]


async def test_history_for_another_spaces_commitment_reads_as_absent(
    commitments,
) -> None:
    """Not an error: a caller must not be able to probe which ids exist elsewhere."""

    first = await commitments.apply(_commitment_intent("i1"))

    assert await commitments.history("default.bob.default", first.commitment.commitment_id) == []


async def test_projection_state_resets_when_a_revision_lands(commitments) -> None:
    """A projection reflecting the previous revision would answer from a stale
    rendering of a promise that has moved on."""

    first = await commitments.apply(_commitment_intent("i1"))
    await commitments.mark_projected(
        SPACE, first.commitment.commitment_id, 1, targets={"drawer", "kg"}
    )

    await commitments.apply(
        _commitment_intent(
            "i2",
            operation="update",
            status="confirmed",
            target_id=first.commitment.commitment_id,
        )
    )

    stored = await commitments.get(SPACE, first.commitment.commitment_id)
    assert stored.drawer_projection_state == "pending"
    assert stored.kg_projection_state == "pending"


async def test_marking_a_superseded_revision_is_refused(commitments) -> None:
    """A projection that finished after the commitment changed must not mark
    stale output as current."""

    first = await commitments.apply(_commitment_intent("i1"))
    await commitments.apply(
        _commitment_intent(
            "i2",
            operation="update",
            status="confirmed",
            target_id=first.commitment.commitment_id,
        )
    )

    with pytest.raises(CommitmentConflict):
        await commitments.mark_projected(
            SPACE, first.commitment.commitment_id, 1, targets={"drawer"}
        )

