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
from eidolon.memory.domain.ports import ExtractionDecisionStore
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

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
