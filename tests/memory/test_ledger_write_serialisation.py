"""Ledger writes queue in the event loop, not inside SQLite.

Every ledger runs its statements through ``asyncio.to_thread``. Without a lock,
two concurrent writers to the same file meet inside SQLite and the loser waits out
``busy_timeout`` — up to five seconds — while holding a thread pool worker.

That was survivable while a process served one space, because the pool was that
space's own. Once one process serves several the pool is shared, so one owner's
write contention would occupy workers every other owner needs. The contention
does not grow (each space has its own files); its cost stops being local.

These tests are about that boundary. They are not about whether the ledgers
store things correctly — test_ledger_contract covers that against both storages.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.command_status import CommandStatusLedger
from eidolon.memory.infrastructure.commitments import CommitmentLedger
from eidolon.memory.infrastructure.dlq import DlqLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger
from eidolon.memory.infrastructure.sqlite_writes import SerialisedSqliteWrites
from eidolon.memory.infrastructure.sync_ledger import SyncLedger

SPACE = "default.alice.default"

LEDGERS = [
    ("canonical_facts", lambda p: CanonicalFactLedger(p)),
    ("commitments", lambda p: CommitmentLedger(p)),
    ("command_status", lambda p: CommandStatusLedger(p, space_id=SPACE)),
    ("dlq", lambda p: DlqLedger(p, space_id=SPACE)),
    ("decisions", lambda p: ExtractionDecisionLedger(p)),
    ("sync", lambda p: SyncLedger(p, space_id=SPACE)),
]


@pytest.mark.parametrize(
    ("name", "build"), LEDGERS, ids=[name for name, _ in LEDGERS]
)
def test_every_ledger_serialises_its_writes(name: str, build, tmp_path: Path) -> None:
    """A ledger without the lock would contend inside SQLite instead."""

    ledger = build(tmp_path / f"{name}.sqlite3")

    assert isinstance(ledger, SerialisedSqliteWrites)
    # Built in __init__, because the first write must not have to create it.
    assert ledger._write_lock is not None


@pytest.mark.parametrize(
    ("name", "build"), LEDGERS, ids=[name for name, _ in LEDGERS]
)
def test_no_ledger_reaches_the_thread_pool_directly(
    name: str, build, tmp_path: Path
) -> None:
    """``asyncio.to_thread`` in a ledger body means that path bypassed the queue.

    Checked against the source rather than behaviour: a bypassed write is only
    visibly wrong under contention, which a unit test cannot reliably produce.
    """

    source = inspect.getsource(type(build(tmp_path / f"{name}.sqlite3")))

    assert "asyncio.to_thread" not in source, (
        f"{name} calls to_thread directly; use self._write or self._read so the "
        f"wait happens in the event loop"
    )


async def test_a_second_writer_waits_for_the_first(tmp_path: Path) -> None:
    """The serialisation itself: two writes never overlap inside the database."""

    ledger = DlqLedger(tmp_path / "dlq.sqlite3", space_id=SPACE)
    overlaps = 0
    inside = 0
    original = ledger._add_sync

    def _tracked(*args):
        nonlocal overlaps, inside
        inside += 1
        if inside > 1:
            overlaps += 1
        try:
            return original(*args)
        finally:
            inside -= 1

    ledger._add_sync = _tracked

    await asyncio.gather(
        *[
            ledger.add(subject="s", payload=f"p{i}".encode(), error="e", deliveries=1)
            for i in range(8)
        ]
    )

    assert overlaps == 0, "two writers were inside the database at once"
    assert len(await ledger.list()) == 8


async def test_reads_are_still_concurrent(tmp_path: Path) -> None:
    """WAL allows one writer alongside many readers, and locking reads would
    give that up for nothing.

    Asserted by observing overlap: if reads queued, the maximum concurrency seen
    would be 1.
    """

    ledger = DlqLedger(tmp_path / "dlq.sqlite3", space_id=SPACE)
    entry = await ledger.add(subject="s", payload=b"p", error="e", deliveries=1)

    peak = 0
    inside = 0
    original = ledger._get_sync

    def _tracked(*args):
        nonlocal peak, inside
        inside += 1
        peak = max(peak, inside)
        try:
            return original(*args)
        finally:
            inside -= 1

    ledger._get_sync = _tracked

    await asyncio.gather(*[ledger.get(entry.entry_id) for _ in range(8)])

    assert peak > 1, "reads were serialised; WAL's one-writer-many-readers is lost"


async def test_two_spaces_do_not_wait_on_each_other(tmp_path: Path) -> None:
    """Each space has its own file and its own lock, so the queue is per space.

    This is why one process serving many spaces does not create new contention —
    only a shared cost for contention that already existed within a space.
    """

    alice = DlqLedger(tmp_path / "alice" / "dlq.sqlite3", space_id="default.alice.default")
    bob = DlqLedger(tmp_path / "bob" / "dlq.sqlite3", space_id="default.bob.default")

    assert alice._write_lock is not bob._write_lock

    await asyncio.gather(
        alice.add(subject="s", payload=b"a", error="e", deliveries=1),
        bob.add(subject="s", payload=b"b", error="e", deliveries=1),
    )

    assert len(await alice.list()) == 1
    assert len(await bob.list()) == 1
