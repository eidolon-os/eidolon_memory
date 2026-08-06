"""The ledgers' table shapes and statements, defined once.

Six ledgers share this module: extraction decisions, canonical facts,
commitments, command status, the dead-letter queue and device sync. Their column
names, primary keys and filter predicates living in one file is what makes them
provably the same shape rather than six files reviewed as similar — and two of
them are product behaviour rather than bookkeeping, so a drift there is a
user-visible bug, not an inconsistency.

**Correction, 2026-08-06.** This opened "shared by both ledger implementations"
and described what differs between them: a parameter marker (``?`` against
SQLite, ``%s`` against PostgreSQL), a dialect-specific ignore-duplicate clause,
and column types SQLite does not distinguish. There is one implementation. The
PostgreSQL ledgers were deleted in ``3fc70e0`` under the local-only decision, and
this file was never revised after — so every statement carried a ``{m}``
placeholder and every use went through ``render()``, all of it resolving to ``?``.

Inlined, for the same reason as ``adapters/kg_sql.py``: a second store would need
this module reopened anyway, since the DDL is SQLite-flavoured throughout. The
placeholder bought a fraction of a hypothetical migration at the cost of every
statement being one indirection from readable, and every reader wondering who the
second implementation was.
"""

from __future__ import annotations

class LedgerSchemaOutdated(RuntimeError):
    """A ledger file predates a column the current statements require.

    Raised instead of letting the query fail. ``CREATE TABLE IF NOT EXISTS`` does
    not alter an existing table, so a file written before ``memory_space_id`` was
    added still parses, still opens, and then fails on the first statement with
    ``no such column`` — from inside a constructor, which takes the whole space
    down rather than one request.

    The message names the file so an operator can act on it. There is no automatic
    migration: this project does not carry historical data forward, and silently
    rewriting a ledger that still holds rows would decide on their behalf.
    """


def ensure_ledger_schema_current(
    conn,
    *,
    table: str,
    required_column: str,
    path,
    rebuildable: bool = False,
) -> None:
    """Rebuild an outdated table when its rows are expendable; refuse otherwise.

    Empty is always safe: nothing is lost, and failing would block a deployment
    over a file with no content.

    ``rebuildable`` marks a table whose rows the design already treats as
    losable. Command status is the case — it is a projection of the command
    stream, and its documented failure mode is that a lost final status shows a
    command as ``accepted`` again, never that an unapplied one looks successful.
    Refusing to start over rows like that would be strictness with no safety
    behind it.

    Everything else is the operator's call. Dead letters are failed turns worth
    inspecting and sync events are what stop a device replaying itself, so
    dropping either silently would be destroying data to avoid an error message.
    """

    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not existing or required_column in existing:
        return

    rows = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    if rows and not rebuildable:
        raise LedgerSchemaOutdated(
            f"{path} holds {rows} row(s) in {table!r} without the "
            f"{required_column!r} column this version requires. This project does "
            f"not migrate historical data — inspect the file and delete it to "
            f"start clean, or keep it aside if the rows matter."
        )

    conn.execute(f"DROP TABLE {table}")


def placeholders(count: int, marker: str) -> str:
    """``?, ?, ?`` or ``%s, %s, %s`` for an IN clause or a VALUES list."""
    return ", ".join([marker] * count)


# ── extraction decisions ─────────────────────────────────────────────────────
#
# Validated steward output, kept so re-processing a turn reaches the same
# conclusion instead of asking the model again. Its primary key is the
# idempotency claim: one decision per (space, turn, extractor version).

EXTRACTION_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_decisions (
    memory_space_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    intents_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_space_id, source_turn_id, extractor_version)
)
"""

EXTRACTION_DECISION_COLUMNS = (
    "memory_space_id",
    "source_turn_id",
    "extractor_version",
    "input_hash",
    "decision_json",
    "intents_json",
    "created_at",
)
"""Selected explicitly rather than with ``*``.

The two implementations return rows differently, and a positional read of
``SELECT *`` would silently reorder if either database chose to.
"""

EXTRACTION_DECISION_SELECT = f"""
SELECT {", ".join(EXTRACTION_DECISION_COLUMNS)}
FROM extraction_decisions
WHERE memory_space_id = ? AND source_turn_id = ? AND extractor_version = ?
"""

EXTRACTION_DECISION_INSERT = f"""
INSERT INTO extraction_decisions ({", ".join(EXTRACTION_DECISION_COLUMNS)})
VALUES ({", ".join(["?"] * len(EXTRACTION_DECISION_COLUMNS))})
"""


# ── device sync events ───────────────────────────────────────────────────────
#
# Which offline batches have already been applied, so a device that retries does
# not replay turns into memory a second time.
#
# ``memory_space_id`` is on the table even though a palace holds exactly one
# space and could rely on the file path for isolation. On shared storage every
# space is in this one table, so without the column one space's sync history
# would answer another's questions — and a column present in only one dialect is
# how the two stop being the same design. Costing a redundant column locally is
# the cheaper side of that trade.

SYNC_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_events (
    memory_space_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    idempotency_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (memory_space_id, event_id)
)
"""

SYNC_EVENTS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_idempotency
ON sync_events(memory_space_id, idempotency_hash)
"""
"""Scoped to the space, so two spaces may legitimately hash to the same batch.

Unique on the hash alone — which is what a per-palace file gave for free — would
make one space's batch look already-applied to another and silently drop its
turns.
"""

SYNC_EVENT_COLUMNS = (
    "memory_space_id",
    "event_id",
    "device_id",
    "instance_id",
    "turn_id",
    "idempotency_hash",
    "status",
    "synced_at",
)

SYNC_EVENT_SEEN = """
SELECT 1 FROM sync_events
WHERE memory_space_id = ? AND (event_id = ? OR idempotency_hash = ?)
"""

SYNC_EVENT_INSERT = f"""
INSERT INTO sync_events ({", ".join(SYNC_EVENT_COLUMNS)})
VALUES ({", ".join(["?"] * len(SYNC_EVENT_COLUMNS))})
"""


# ── dead letters ─────────────────────────────────────────────────────────────
#
# Turns the service could not process, kept so they can be inspected and
# replayed. Carries a space column for the same reason sync does: on shared
# storage one owner must not see another's failed turns, and the payload here is
# a whole conversation turn.

DLQ_ENTRIES_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS dlq_entries (
    memory_space_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload {blob} NOT NULL,
    error TEXT NOT NULL,
    deliveries INTEGER NOT NULL,
    state TEXT NOT NULL,
    replay_attempts INTEGER NOT NULL DEFAULT 0,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_space_id, entry_id)
)
"""
"""``{blob}`` is the only type that differs: SQLite BLOB, PostgreSQL BYTEA."""

DLQ_ENTRIES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_dlq_state_updated
ON dlq_entries(memory_space_id, state, updated_at)
"""

DLQ_STATES = frozenset({"unresolved", "replaying", "replayed", "resolved"})

DLQ_COLUMNS = (
    "entry_id",
    "subject",
    "payload",
    "error",
    "deliveries",
    "state",
    "replay_attempts",
    "resolution_note",
    "created_at",
    "updated_at",
)

_DLQ_SELECT = f"SELECT {', '.join(DLQ_COLUMNS)} FROM dlq_entries"

DLQ_SELECT_ONE = f"{_DLQ_SELECT} WHERE memory_space_id = ? AND entry_id = ?"

DLQ_SELECT_PAGE = f"""
{_DLQ_SELECT}
WHERE memory_space_id = ?
ORDER BY created_at DESC LIMIT ? OFFSET ?
"""

DLQ_SELECT_PAGE_BY_STATE = f"""
{_DLQ_SELECT}
WHERE memory_space_id = ? AND state = ?
ORDER BY created_at DESC LIMIT ? OFFSET ?
"""

DLQ_INSERT = """
INSERT INTO dlq_entries (
    memory_space_id, entry_id, subject, payload, error, deliveries, state,
    replay_attempts, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, 'unresolved', 0, ?, ?)
"""

DLQ_CLAIM = """
UPDATE dlq_entries
SET state = 'replaying', replay_attempts = replay_attempts + 1, updated_at = ?
WHERE memory_space_id = ? AND entry_id = ? AND state = 'unresolved'
"""
"""Claiming is one conditional UPDATE, not a read followed by a write.

The state predicate is what makes it exclusive: whichever caller's update
matches a row has the claim, and a second caller matches nothing. A read-then-
write would need a lock to be correct, and holding one across a network round
trip is what the shared deployment exists not to do.
"""

DLQ_FINISH_REPLAY = """
UPDATE dlq_entries
SET state = ?, error = COALESCE(?, error), updated_at = ?
WHERE memory_space_id = ? AND entry_id = ? AND state = 'replaying'
"""

DLQ_RESOLVE = """
UPDATE dlq_entries
SET state = 'resolved', resolution_note = ?, updated_at = ?
WHERE memory_space_id = ? AND entry_id = ? AND state != 'replaying'
"""

DLQ_COUNT_BY_STATE = """
SELECT state, COUNT(*) FROM dlq_entries
WHERE memory_space_id = ? GROUP BY state
"""

DLQ_OLDEST_UNRESOLVED = """
SELECT MIN(created_at) FROM dlq_entries
WHERE memory_space_id = ? AND state = 'unresolved'
"""


# ── command status ───────────────────────────────────────────────────────────
#
# A read-optimised projection of where each asynchronous command got to. The
# command stream stays the write path; losing a row here can leave a command
# looking `accepted` after a restart, but can never make an unapplied one look
# successful.
#
# Space column for the same reason as the others: on shared storage one owner
# must not be able to ask about another owner's command.

COMMAND_STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_status (
    memory_space_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    resource_id TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_space_id, request_id)
)
"""

COMMAND_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_command_status_updated
ON command_status(memory_space_id, updated_at)
"""

COMMAND_STATUS_COLUMNS = (
    "request_id",
    "kind",
    "status",
    "resource_id",
    "error",
    "attempts",
    "created_at",
    "updated_at",
)

COMMAND_STATUS_SELECT = f"""
SELECT {", ".join(COMMAND_STATUS_COLUMNS)} FROM command_status
WHERE memory_space_id = ? AND request_id = ?
"""

COMMAND_STATUS_INSERT = """
INSERT INTO command_status (
    memory_space_id, request_id, kind, status, resource_id, error,
    attempts, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

COMMAND_STATUS_UPDATE = """
UPDATE command_status
SET kind = ?, status = ?, resource_id = ?, error = ?,
    attempts = ?, updated_at = ?
WHERE memory_space_id = ? AND request_id = ?
"""

COMMAND_STATUS_COUNT_BY_STATUS = """
SELECT status, COUNT(*) FROM command_status
WHERE memory_space_id = ? GROUP BY status
"""

COMMAND_STATUS_OLDEST_ACTIVE = """
SELECT MIN(created_at) FROM command_status
WHERE memory_space_id = ? AND status IN ('accepted', 'retrying')
"""

COMMAND_STATUS_PRUNE_EXPIRED = """
DELETE FROM command_status
WHERE memory_space_id = ? AND status IN ('applied', 'failed') AND updated_at < ?
"""

COMMAND_STATUS_COUNT_ALL = "SELECT COUNT(*) FROM command_status WHERE memory_space_id = ?"

COMMAND_STATUS_PRUNE_OVERFLOW = """
DELETE FROM command_status
WHERE memory_space_id = ? AND request_id IN (
    SELECT request_id FROM command_status
    WHERE memory_space_id = ? AND status IN ('applied', 'failed')
    ORDER BY updated_at ASC, request_id ASC
    LIMIT ?
)
"""
"""Oldest terminal rows first, so pruning never removes work still in flight.

Ordered by request_id as well as time: two rows updated in the same instant
would otherwise be dropped in whichever order the database happened to return,
making the prune non-deterministic between the two dialects.
"""


# ── commitments ──────────────────────────────────────────────────────────────
#
# Promises still in play, plus an append-only revision history. This is product
# behaviour, not bookkeeping: it is what commitment queries are answered from, so
# a deployment without it tells the user there are no promises rather than
# admitting it cannot see them.
#
# Two tables. The revision row is what makes an intent idempotent — replaying one
# finds its revision and returns the stored decision instead of applying twice.

COMMITMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    promisor TEXT NOT NULL,
    predicate TEXT NOT NULL,
    action_value TEXT NOT NULL,
    beneficiaries_json TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    condition_value TEXT,
    due_at TEXT,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    drawer_projection_state TEXT NOT NULL DEFAULT 'pending',
    kg_projection_state TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (memory_space_id, commitment_id)
)
"""

COMMITMENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_commitments_realm_status
ON commitments(memory_space_id, status, updated_at)
"""

COMMITMENT_REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS commitment_revisions (
    revision_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL,
    intent_id TEXT UNIQUE NOT NULL,
    intent_hash TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    authority TEXT NOT NULL,
    operation TEXT NOT NULL,
    previous_status TEXT,
    status TEXT NOT NULL,
    raw_claim TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""
"""No foreign key to ``commitments``.

The SQLite version had one, but the parent key is now (space, commitment_id)
while a revision only carries the commitment id — and adding the space to
revisions to satisfy a constraint would be letting the constraint shape the data.
The write path inserts the parent first inside one transaction, which is what
actually guarantees the relationship.
"""

COMMITMENT_REVISIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_commitment_revisions_commitment
ON commitment_revisions(commitment_id, recorded_at)
"""

COMMITMENT_COLUMNS = (
    "commitment_id",
    "memory_space_id",
    "promisor",
    "predicate",
    "action_value",
    "beneficiaries_json",
    "participants_json",
    "condition_value",
    "due_at",
    "status",
    "revision",
    "created_at",
    "updated_at",
    "drawer_projection_state",
    "kg_projection_state",
)

COMMITMENT_REVISION_COLUMNS = (
    "revision_id",
    "commitment_id",
    "intent_id",
    "intent_hash",
    "source_event_id",
    "authority",
    "operation",
    "previous_status",
    "status",
    "raw_claim",
    "snapshot_json",
    "recorded_at",
)

_COMMITMENT_SELECT = f"SELECT {', '.join(COMMITMENT_COLUMNS)} FROM commitments"

COMMITMENT_SELECT_ONE = f"""
{_COMMITMENT_SELECT}
WHERE memory_space_id = ? AND commitment_id = ?
"""

COMMITMENT_SELECT_BY_ID = f"{_COMMITMENT_SELECT} WHERE commitment_id = ?"

# The 13 written columns; the two projection states default to 'pending'.
COMMITMENT_INSERT = f"""
INSERT INTO commitments ({", ".join(COMMITMENT_COLUMNS[:13])})
VALUES ({", ".join(["?"] * 13)})
"""

COMMITMENT_UPDATE = """
UPDATE commitments
SET participants_json = ?, condition_value = ?, due_at = ?,
    status = ?, revision = ?, updated_at = ?,
    drawer_projection_state = 'pending',
    kg_projection_state = 'pending'
WHERE commitment_id = ? AND memory_space_id = ?
"""
"""Any change resets both projections to pending.

A revision whose drawer or graph projection still reflects the previous one would
have the service answer from a stale rendering of a promise that has moved on.
"""

COMMITMENT_COUNT = "SELECT COUNT(*) FROM commitments WHERE memory_space_id = ?"

COMMITMENT_SELECT_PAGE = f"""
{_COMMITMENT_SELECT}
WHERE memory_space_id = ?
ORDER BY updated_at DESC LIMIT ?
"""

# Soonest due first, undated last, then most recently touched. Ordering by the
# text rather than a date function: due_at is ISO 8601, whose lexicographic order
# is its chronological order, and SQLite's julianday() has no PostgreSQL
# equivalent. Pinned by test_commitment_page_orders_by_due_date.
_COMMITMENT_ACTIVE_ORDER = """
ORDER BY
    CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
    due_at ASC,
    updated_at DESC,
    commitment_id ASC
"""


def commitment_count_active(marker: str, status_count: int) -> str:
    return (
        "SELECT COUNT(*) FROM commitments WHERE memory_space_id = "
        f"{marker} AND status IN ({placeholders(status_count, marker)})"
    )


def commitment_select_active_page(marker: str, status_count: int) -> str:
    return (
        f"{_COMMITMENT_SELECT} WHERE memory_space_id = {marker} "
        f"AND status IN ({placeholders(status_count, marker)})"
        f"{_COMMITMENT_ACTIVE_ORDER} LIMIT {marker}"
    )


def commitment_mark_projected(columns: list[str]) -> str:
    """Mark named projections done, only if the revision has not moved on.

    The revision predicate is the guard: a projection that finished after the
    commitment changed would otherwise mark stale output as current.
    """

    assignments = ", ".join(f"{column} = 'projected'" for column in sorted(columns))
    return (
        f"UPDATE commitments SET {assignments} WHERE memory_space_id = ? "
        "AND commitment_id = ? AND revision = ?"
    )


COMMITMENT_REVISION_INSERT = f"""
INSERT INTO commitment_revisions ({", ".join(COMMITMENT_REVISION_COLUMNS)})
VALUES ({", ".join(["?"] * len(COMMITMENT_REVISION_COLUMNS))})
"""

COMMITMENT_REVISION_BY_INTENT = f"""
SELECT {", ".join(COMMITMENT_REVISION_COLUMNS)} FROM commitment_revisions
WHERE intent_id = ?
"""

COMMITMENT_REVISION_BY_ID = f"""
SELECT {", ".join(COMMITMENT_REVISION_COLUMNS)} FROM commitment_revisions
WHERE revision_id = ?
"""

COMMITMENT_REVISION_HISTORY = f"""
SELECT {", ".join(COMMITMENT_REVISION_COLUMNS)} FROM commitment_revisions
WHERE commitment_id = ?
ORDER BY recorded_at DESC, revision_id DESC LIMIT ?
"""


# ── canonical facts ──────────────────────────────────────────────────────────
#
# The chain that makes a corrected fact stop being recalled. An assertion is one
# (subject, predicate, object) the owner has confirmed; evidence rows are the
# turns that confirmed it; invalidations and reactivations are how it leaves and
# returns to being current.
#
# This is product behaviour, not bookkeeping. Without it a fact the owner
# corrected keeps being recalled, which is the failure a user reads as their
# companion not listening.
#
# Every type here is TEXT or INTEGER except one REAL, which both databases
# accept — so unlike the DLQ's blob there is nothing to template. Sharing the
# schema is what stops the two from disagreeing on a column name or a
# constraint, which would not look like a bug so much as cloud answering
# differently from local.

CANONICAL_ASSERTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_assertions (
    assertion_id TEXT PRIMARY KEY,
    memory_space_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_value TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    drawer_projection_state TEXT NOT NULL DEFAULT 'pending',
    kg_projection_state TEXT NOT NULL DEFAULT 'pending',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    activation_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(memory_space_id, subject, predicate, object_value)
)
"""
"""The UNIQUE constraint is the identity claim: one assertion per fact per space.

``assertion_id`` is derived from those same four values, so the constraint is
what makes a second derivation collide rather than duplicate.
"""

CANONICAL_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_evidence (
    intent_id TEXT PRIMARY KEY,
    memory_space_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    tool_call_id TEXT,
    authority TEXT NOT NULL,
    raw_claim TEXT NOT NULL,
    confidence REAL NOT NULL,
    occurred_at TEXT,
    recorded_at TEXT NOT NULL
)
"""
"""``intent_id`` as the primary key is the idempotency claim.

No foreign key to the assertion, unlike the SQLite original: the write path
inserts the assertion first inside one transaction, which is what actually
guarantees the relationship, and a constraint here would only add a way for a
replica's transaction to fail on ordering.
"""

CANONICAL_EVIDENCE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_canonical_evidence_assertion
ON canonical_evidence(assertion_id, recorded_at)
"""

CANONICAL_INVALIDATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_invalidations (
    intent_id TEXT PRIMARY KEY,
    memory_space_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    raw_claim TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    result_state TEXT NOT NULL DEFAULT 'invalidated'
)
"""
"""``state`` tracks the request, ``result_state`` what it decided.

Separate because an invalidation is recorded before it is applied: a request
still pending and one that concluded the fact was superseded are different
things, and one column could not say both.
"""

CANONICAL_INVALIDATIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_canonical_invalidations_assertion
ON canonical_invalidations(assertion_id, recorded_at)
"""

CANONICAL_REACTIVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_reactivations (
    intent_id TEXT PRIMARY KEY,
    memory_space_id TEXT NOT NULL,
    assertion_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    raw_claim TEXT NOT NULL,
    reactivated_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    prior_state TEXT NOT NULL,
    activation_number INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
)
"""
"""``activation_number`` is what makes a projection identifiable.

A fact invalidated and later reconfirmed needs its second life projected
separately from its first, or a stale drawer from the first would be treated as
current.
"""

CANONICAL_REACTIVATIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_canonical_reactivations_assertion
ON canonical_reactivations(assertion_id, recorded_at)
"""


def canonical_schema() -> tuple[str, ...]:
    """Every statement needed to create the canonical-fact tables, in order.

    Returned as a sequence rather than one script because PostgreSQL's driver
    executes one statement per call, and SQLite's ``executescript`` would commit
    an open transaction out from under a caller.
    """

    return (
        CANONICAL_ASSERTIONS_SCHEMA,
        CANONICAL_EVIDENCE_SCHEMA,
        CANONICAL_EVIDENCE_INDEX,
        CANONICAL_INVALIDATIONS_SCHEMA,
        CANONICAL_INVALIDATIONS_INDEX,
        CANONICAL_REACTIVATIONS_SCHEMA,
        CANONICAL_REACTIVATIONS_INDEX,
    )
