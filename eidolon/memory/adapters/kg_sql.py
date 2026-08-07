"""The graph's schema and query shapes, defined once.

The SQL lives here rather than inside the adapter so that ``kg_sqlite`` reads as
orchestration — take the lock, bind the parameters, map the rows — with the shapes
it orchestrates stated in one place. That is worth doing for one implementation
and it is why this module stays.

**Correction, 2026-08-06.** It used to open "shared by the graph's two storage
implementations", and describe the dialect differences as "narrow and named
explicitly — a parameter marker and an upsert clause". There is one implementation.
``kg_postgres.py`` was added in ``729ec17`` for multi-host replicas and deleted in
``3fc70e0`` under the local-only decision, together with the dialect tests; this
file was never revised after its second consumer went away. The upsert clause it
named lived in the deleted file and has no counterpart here, and the parameter
marker was threaded through ``audience_filter`` and ``VALID_AT`` to serve a caller
that no longer exists. Both are now inlined as ``?``.

The generality is not kept "in case". A second store would need this module
reopened either way — the schema DDL is SQLite-flavoured throughout — so carrying a
marker parameter bought a fraction of that at the cost of every reader wondering
who the other implementation was.

The queries are deliberately unremarkable: equality on a subject, an interval
test, an audience filter, a bounded order. That is the entire graph workload this
service has, which is why a relational store is the right answer and why a graph
database would be buying capability we never exercise.

Timestamps are stored as ISO-8601 text. Text loses the range operators a native
timestamp type gives, but ISO-8601 in UTC compares correctly as a string, and one
representation everywhere keeps the interval predicate below ordinary SQL. A
date-only value is normalised on the way in rather than being special-cased in
every comparison.
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
    #
    # ``object_id`` is here for the *write* path, and it is the difference between
    # a lookup and a scan. ``add_triple`` runs two idempotency probes before it
    # inserts, both keyed on the full triple:
    #
    #     WHERE space_id=? AND source_turn_id=? AND subject_id=? AND predicate=? AND object_id=?
    #     WHERE space_id=? AND subject_id=? AND predicate=? AND object_id=? AND valid_to IS NULL
    #
    # Without the fourth column SQLite finds every row for that subject and
    # predicate and then filters, so both probes cost the size of that group.
    # Measured on a Raspberry Pi 5 against a graph whose hot subject held 100 198
    # statements, 15 199 of them under one predicate — which is the shape a
    # companion's graph actually has, since "用户" is the subject of most of it:
    #
    #     probe                  before      after
    #     idempotency by turn    13.65 ms    (a lookup)
    #     still-valid dedup      13.38 ms    (a lookup)
    #     one add_triple         28.44 ms
    #
    # **This is on the turn path**, so it was the graph's slowest remaining
    # operation and the only one a user could feel as the companion falling
    # behind. Reads had all been made flat; the write had not, and nobody had
    # looked because the probe that was supposed to watch it was measuring
    # something else (see ``probe_kg_scale``).
    #
    # A prefix extension, so every existing reader of the three-column form is
    # unaffected.
    #
    # **Renamed, and the old name dropped, because otherwise no existing palace
    # would ever get this.** ``CREATE INDEX IF NOT EXISTS`` matches on the name
    # alone: had the column list changed under the old name, every database that
    # already had the three-column index would skip the statement and keep the
    # slow one, silently, forever — the fix would work only on graphs created
    # after it. The drop is a no-op once it has run.
    """
    DROP INDEX IF EXISTS idx_kg_statements_subject
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kg_statements_triple
        ON kg_statements (space_id, subject_id, predicate, object_id)
    """,
    # The recall path's *ordering*, which the index above does not provide.
    #
    # Recall wants the best few statements about a subject, and the ranking is
    # ``confidence DESC, valid_from DESC, recorded_at DESC``. With only the index
    # above, SQLite finds the subject's rows quickly and then sorts all of them
    # before the LIMIT can discard any — which for a companion's hot subject is
    # most of the graph. Measured at 20 000 statements: 33 ms to return 8 rows,
    # because "用户" held 13 333 of them.
    #
    # Column order mirrors the ORDER BY exactly, DESC included, so a bounded query
    # walks the index and stops. That turns top-N-per-subject from O(rows for that
    # subject) into O(log n + N).
    """
    CREATE INDEX IF NOT EXISTS idx_kg_statements_relevance
        ON kg_statements (
            space_id, subject_id, confidence DESC, valid_from DESC, recorded_at DESC
        )
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
    # Entity-name matching, which is a scan and stays one.
    #
    # ``match_entities_for_query`` asks whether a *stored* name occurs in the
    # query text, so the wildcard is on the stored side and no index can turn it
    # into a lookup. That was the reason given on 2026-08-06 for leaving it linear
    # and calling a trigram FTS table the only real fix.
    #
    # **That reasoning conflated two things.** The scan cannot be removed; the
    # *table lookup inside it* can. Without this index SQLite walks the primary
    # key — ``(space_id, entity_id)`` — and fetches each row to read ``name``.
    # With it the whole predicate is answered from index pages:
    #
    #     without   SEARCH kg_entities USING INDEX sqlite_autoindex_kg_entities_1
    #     with      SEARCH kg_entities USING COVERING INDEX idx_kg_entities_name
    #
    # Measured through the adapter, index built and dropped again to rule out a
    # warm cache: 2.10 → 0.96 ms at 6 668 entities, 8.50 → 3.12 at 25 001,
    # 19.08 → 6.45 at 45 001. The ratio grows with the table (2.2x, 2.7x, 3.0x)
    # because the row fetch is what scales and the index-only walk is not.
    #
    # Still linear, and a trigram table is still the only thing that would change
    # that. This is the cheaper three-fold that should have been taken first.
    """
    CREATE INDEX IF NOT EXISTS idx_kg_entities_name
        ON kg_entities (space_id, name)
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
    (s.valid_from IS NULL OR s.valid_from <= ?)
    AND (s.valid_to IS NULL OR s.valid_to > ?)
"""
"""Whether a statement holds at a point in time.

Half-open: a statement is valid from its start up to but excluding its end, so
superseding one with another at the same instant leaves no moment where both are
true and none where neither is.

NULL means unbounded — no recorded start is treated as "always has been", no
recorded end as "still is".
"""


def audience_filter(count: int) -> str:
    """An IN clause over the audiences a caller may read.

    Built from the *count* only; the tokens themselves are always bound
    parameters, never interpolated.
    """

    placeholders = ", ".join("?" for _ in range(count))
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

# ``SUBJECT_RANK`` and ``RANKED_SUBJECT_COLUMNS`` lived here: a
# ``ROW_NUMBER() OVER (PARTITION BY subject_id)`` that gave each subject its own
# allowance in one statement. Correct, and removed on 2026-08-06 because a window
# function has to rank a whole partition before anything can be discarded — 33 ms
# to return 8 rows once a companion's hot subject held 13 333 statements.
#
# ``query_subjects`` now unions one bounded branch per subject, each walking
# ``idx_kg_statements_relevance`` and stopping at its LIMIT. The per-subject bound
# is unchanged; only its cost is.


def name_appears_in(name: str, query: str) -> bool:
    """Whether a canonical entity name is referred to by a piece of text.

    Whole first, then the tail after a type prefix — the steward writes
    ``pet:铁锤`` to keep a dog distinct from a person of the same name, while
    someone asking about the dog just says 铁锤.

    A name that is nothing but a prefix (``pet:``) matches nothing, rather than
    matching every query that happens to contain a colon.
    """

    if not name:
        return False
    if name in query:
        return True
    if ":" in name:
        tail = name.split(":", 1)[1]
        return bool(tail) and tail in query
    return False
