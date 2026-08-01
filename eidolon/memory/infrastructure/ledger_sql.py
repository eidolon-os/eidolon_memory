"""Table shapes and statements shared by both ledger implementations.

A ledger exists twice: as a SQLite file inside a palace, and as rows in a shared
database that any replica can reach. Those are two deployments of one design, so
the design lives here and each implementation supplies only what its database
does differently.

What differs is small and known:

* the parameter marker — ``?`` against SQLite, ``%s`` against PostgreSQL;
* how an insert is told to ignore a duplicate;
* a handful of column types SQLite does not distinguish.

What must not differ is the shape: column names, primary keys, and the
predicates a query filters on. A drift there does not look like a bug — it looks
like cloud answering differently from local, discovered by a user rather than a
test. Keeping the statements in one place is what makes the two provably the same
rather than reviewed as similar.

Statements are templates: ``{m}`` is the marker, substituted by the
implementation. They are deliberately not built by string concatenation at call
time, so the shape can be compared between dialects without running anything.
"""

from __future__ import annotations

SQLITE_MARKER = "?"
POSTGRES_MARKER = "%s"


def render(template: str, marker: str) -> str:
    """Fill a statement template's parameter markers for one dialect."""
    return template.replace("{m}", marker)


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
WHERE memory_space_id = {{m}} AND source_turn_id = {{m}} AND extractor_version = {{m}}
"""

EXTRACTION_DECISION_INSERT = f"""
INSERT INTO extraction_decisions ({", ".join(EXTRACTION_DECISION_COLUMNS)})
VALUES ({", ".join(["{m}"] * len(EXTRACTION_DECISION_COLUMNS))})
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
WHERE memory_space_id = {m} AND (event_id = {m} OR idempotency_hash = {m})
"""

SYNC_EVENT_INSERT = f"""
INSERT INTO sync_events ({", ".join(SYNC_EVENT_COLUMNS)})
VALUES ({", ".join(["{m}"] * len(SYNC_EVENT_COLUMNS))})
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

DLQ_SELECT_ONE = f"{_DLQ_SELECT} WHERE memory_space_id = {{m}} AND entry_id = {{m}}"

DLQ_SELECT_PAGE = f"""
{_DLQ_SELECT}
WHERE memory_space_id = {{m}}
ORDER BY created_at DESC LIMIT {{m}} OFFSET {{m}}
"""

DLQ_SELECT_PAGE_BY_STATE = f"""
{_DLQ_SELECT}
WHERE memory_space_id = {{m}} AND state = {{m}}
ORDER BY created_at DESC LIMIT {{m}} OFFSET {{m}}
"""

DLQ_INSERT = """
INSERT INTO dlq_entries (
    memory_space_id, entry_id, subject, payload, error, deliveries, state,
    replay_attempts, created_at, updated_at
) VALUES ({m}, {m}, {m}, {m}, {m}, {m}, 'unresolved', 0, {m}, {m})
"""

DLQ_CLAIM = """
UPDATE dlq_entries
SET state = 'replaying', replay_attempts = replay_attempts + 1, updated_at = {m}
WHERE memory_space_id = {m} AND entry_id = {m} AND state = 'unresolved'
"""
"""Claiming is one conditional UPDATE, not a read followed by a write.

The state predicate is what makes it exclusive: whichever caller's update
matches a row has the claim, and a second caller matches nothing. A read-then-
write would need a lock to be correct, and holding one across a network round
trip is what the shared deployment exists not to do.
"""

DLQ_FINISH_REPLAY = """
UPDATE dlq_entries
SET state = {m}, error = COALESCE({m}, error), updated_at = {m}
WHERE memory_space_id = {m} AND entry_id = {m} AND state = 'replaying'
"""

DLQ_RESOLVE = """
UPDATE dlq_entries
SET state = 'resolved', resolution_note = {m}, updated_at = {m}
WHERE memory_space_id = {m} AND entry_id = {m} AND state != 'replaying'
"""

DLQ_COUNT_BY_STATE = """
SELECT state, COUNT(*) FROM dlq_entries
WHERE memory_space_id = {m} GROUP BY state
"""

DLQ_OLDEST_UNRESOLVED = """
SELECT MIN(created_at) FROM dlq_entries
WHERE memory_space_id = {m} AND state = 'unresolved'
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
WHERE memory_space_id = {{m}} AND request_id = {{m}}
"""

COMMAND_STATUS_INSERT = """
INSERT INTO command_status (
    memory_space_id, request_id, kind, status, resource_id, error,
    attempts, created_at, updated_at
) VALUES ({m}, {m}, {m}, {m}, {m}, {m}, {m}, {m}, {m})
"""

COMMAND_STATUS_UPDATE = """
UPDATE command_status
SET kind = {m}, status = {m}, resource_id = {m}, error = {m},
    attempts = {m}, updated_at = {m}
WHERE memory_space_id = {m} AND request_id = {m}
"""

COMMAND_STATUS_COUNT_BY_STATUS = """
SELECT status, COUNT(*) FROM command_status
WHERE memory_space_id = {m} GROUP BY status
"""

COMMAND_STATUS_OLDEST_ACTIVE = """
SELECT MIN(created_at) FROM command_status
WHERE memory_space_id = {m} AND status IN ('accepted', 'retrying')
"""

COMMAND_STATUS_PRUNE_EXPIRED = """
DELETE FROM command_status
WHERE memory_space_id = {m} AND status IN ('applied', 'failed') AND updated_at < {m}
"""

COMMAND_STATUS_COUNT_ALL = "SELECT COUNT(*) FROM command_status WHERE memory_space_id = {m}"

COMMAND_STATUS_PRUNE_OVERFLOW = """
DELETE FROM command_status
WHERE memory_space_id = {m} AND request_id IN (
    SELECT request_id FROM command_status
    WHERE memory_space_id = {m} AND status IN ('applied', 'failed')
    ORDER BY updated_at ASC, request_id ASC
    LIMIT {m}
)
"""
"""Oldest terminal rows first, so pruning never removes work still in flight.

Ordered by request_id as well as time: two rows updated in the same instant
would otherwise be dropped in whichever order the database happened to return,
making the prune non-deterministic between the two dialects.
"""
