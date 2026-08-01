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
