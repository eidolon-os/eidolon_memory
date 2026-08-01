"""Schema and query shapes shared by the graph's two storage implementations.

Both stores answer the same questions, so the SQL lives here once rather than
diverging in two files that drift. The dialect differences are narrow and named
explicitly — a parameter marker and an upsert clause — so a reader can see the
whole of what differs.

The queries are deliberately unremarkable: equality on a subject, an interval
test, an audience filter, a bounded order. That is the entire graph workload this
service has, which is why a relational store is the right answer and why a graph
database would be buying capability we never exercise.

Timestamps are stored as ISO-8601 text, in both stores. Text loses the range
operators a native timestamp type gives, but ISO-8601 in UTC compares correctly
as a string, and using one representation everywhere means the interval predicate
below is literally the same SQL against both. A date-only value is normalised on
the way in rather than being special-cased in every comparison.
"""

from __future__ import annotations

# ── schema ───────────────────────────────────────────────────────────────────
#
# `space_id` is on every table. Locally each space already has its own file, so
# the column is redundant — deliberately so: it is the same defence in depth the
# vector store applies, and it means one shared database and one file per space
# run the identical statements.

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS kg_entities (
        space_id    TEXT NOT NULL,
        entity_id   TEXT NOT NULL,
        name        TEXT NOT NULL,
        entity_type TEXT NOT NULL DEFAULT 'unknown',
        properties  TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL,
        PRIMARY KEY (space_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_statements (
        space_id       TEXT NOT NULL,
        statement_id   TEXT NOT NULL,
        subject_id     TEXT NOT NULL,
        predicate      TEXT NOT NULL,
        object_id      TEXT NOT NULL,
        audience       TEXT NOT NULL,
        sensitive      INTEGER NOT NULL DEFAULT 0,
        valid_from     TEXT,
        valid_to       TEXT,
        recorded_at    TEXT NOT NULL,
        confidence     REAL NOT NULL DEFAULT 1.0,
        source_turn_id TEXT,
        adapter_name   TEXT,
        PRIMARY KEY (space_id, statement_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_entity_mentions (
        space_id   TEXT NOT NULL,
        mention_id TEXT NOT NULL,
        entity_id  TEXT NOT NULL,
        alias      TEXT NOT NULL,
        source     TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.85,
        created_at TEXT NOT NULL,
        PRIMARY KEY (space_id, mention_id)
    )
    """,
    # The recall path's index: subject lookup within a space. Predicate is
    # included because canonical-fact checks ask for a specific one.
    """
    CREATE INDEX IF NOT EXISTS idx_kg_statements_subject
        ON kg_statements (space_id, subject_id, predicate)
    """,
    # Incoming direction — "what points at this entity".
    """
    CREATE INDEX IF NOT EXISTS idx_kg_statements_object
        ON kg_statements (space_id, object_id)
    """,
    # Timeline ordering, and the interval test's leading column.
    """
    CREATE INDEX IF NOT EXISTS idx_kg_statements_validity
        ON kg_statements (space_id, valid_from, valid_to)
    """,
    # Idempotency probe: has this turn already produced this statement?
    """
    CREATE INDEX IF NOT EXISTS idx_kg_statements_source
        ON kg_statements (space_id, source_turn_id)
    """,
    # Alias lookup, for resolving "my dad" to an entity.
    """
    CREATE INDEX IF NOT EXISTS idx_kg_mentions_alias
        ON kg_entity_mentions (space_id, alias)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_mentions_unique
        ON kg_entity_mentions (space_id, entity_id, alias)
    """,
)


# ── predicates ───────────────────────────────────────────────────────────────

VALID_AT = """
    (s.valid_from IS NULL OR s.valid_from <= {p})
    AND (s.valid_to IS NULL OR s.valid_to > {p})
"""
"""Whether a statement holds at a point in time.

Half-open: a statement is valid from its start up to but excluding its end, so
superseding one with another at the same instant leaves no moment where both are
true and none where neither is.

NULL means unbounded — no recorded start is treated as "always has been", no
recorded end as "still is".
"""


def audience_filter(count: int, marker: str) -> str:
    """An IN clause over the audiences a caller may read."""

    placeholders = ", ".join(marker for _ in range(count))
    return f"s.audience IN ({placeholders})"


# Selecting a statement always joins both entity rows, because callers want
# display names rather than the slugs used as keys.
#
# Every column is aliased explicitly. Two of them come from the same column of
# two joined tables, and relying on a driver's automatic disambiguation would
# make the outer projection of a ranked subquery depend on naming rules that
# differ between stores.
SELECT_COLUMNS = """
    s.statement_id  AS statement_id,
    subj.name       AS subject_name,
    s.predicate     AS predicate,
    obj.name        AS object_name,
    s.valid_from    AS valid_from,
    s.valid_to      AS valid_to,
    s.confidence    AS confidence,
    s.source_turn_id AS source_turn_id,
    s.adapter_name  AS adapter_name
"""

JOIN_ENTITIES = """
    FROM kg_statements s
    JOIN kg_entities subj ON subj.space_id = s.space_id AND subj.entity_id = s.subject_id
    JOIN kg_entities obj  ON obj.space_id  = s.space_id AND obj.entity_id  = s.object_id
"""

# Most recent first, with confidence ahead of recency: a fact we are sure of
# outranks a fresher guess.
_RELEVANCE = "s.confidence DESC, s.valid_from DESC, s.recorded_at DESC"

ORDER_BY_RELEVANCE = f"ORDER BY {_RELEVANCE}"

SUBJECT_RANK = f"""
    ROW_NUMBER() OVER (PARTITION BY s.subject_id ORDER BY {_RELEVANCE}) AS subject_rank
"""
"""Rank statements within each subject, so each gets its own allowance.

Needed because the recall read asks about several subjects at once and must not
let one well-connected entity spend the whole budget. Ranking in the query rather
than bucketing the results in Python is also what keeps it to a single round
trip, which matters once the store is across a network.
"""

# The outer projection over the ranked subquery, in the same order as
# SELECT_COLUMNS so a row is read positionally either way.
RANKED_SUBJECT_COLUMNS = """
    ranked.statement_id, ranked.subject_name, ranked.predicate, ranked.object_name,
    ranked.valid_from, ranked.valid_to, ranked.confidence,
    ranked.source_turn_id, ranked.adapter_name
"""
