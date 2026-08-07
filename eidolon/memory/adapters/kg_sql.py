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
    # Entity-name matching, which is now a seek.
    #
    # **This comment has been wrong twice and the history is the useful part.**
    #
    # First it said matching could not be indexed at all: the test asks whether a
    # *stored* name occurs in the query, so the wildcard is on the stored side.
    # True, and it concluded a trigram FTS table was the only fix.
    #
    # Then it said the scan could not be removed but the row fetch inside it
    # could, and this index made the walk index-only — measured 2.10 → 0.96 ms at
    # 6 668 entities, 19.08 → 6.45 at 45 001. Also true, and still linear.
    #
    # Both readings missed that the question itself can be turned around:
    #
    #     name occurs in phrase  ⟺  name equals some substring of phrase
    #
    # Enumerate the phrase's substrings and seek them, and this index answers by
    # equality. Same results, same order, no threshold — and the cost stops
    # depending on the size of the graph, which was the entire problem. Measured
    # at 80 000 entities: 0.09 ms, against 25.69 ms for the scan on the board.
    #
    # Worth recording rather than tidying away: the obstacle was never the index,
    # it was accepting the query's shape as given.
    """
    CREATE INDEX IF NOT EXISTS idx_kg_entities_name
        ON kg_entities (space_id, name)
    """,
    # The same lookup for the tail after a type prefix.
    #
    # ``match_entities_for_query`` accepts ``pet:铁锤`` when someone says 铁锤, so
    # the tail needs the same seek the whole name gets. An expression index gives
    # it one, and ``name`` is carried as a third column deliberately: without it
    # the planner prefers ``idx_kg_entities_name`` and scans the space, because
    # that index at least covers the column being selected.
    """
    CREATE INDEX IF NOT EXISTS idx_kg_entities_tail
        ON kg_entities (space_id, substr(name, instr(name, ':') + 1), name)
    """,
    # Alias lookup, for resolving "my dad" to an entity.
    #
    # ``entity_id`` is the third column so this index *covers* the alias query,
    # and that is the only reason it gets used at all.
    #
    # With ``(space_id, alias)`` the planner preferred ``idx_kg_mentions_unique``
    # — ``(space_id, entity_id, alias)`` — and searched it on ``space_id=?``
    # alone, scanning every mention in the space, because that index at least
    # supplied every column ``m`` contributes and this one would have needed a
    # row fetch per hit to reach ``entity_id`` for the join. A covering scan beat
    # a non-covering seek, which is the same trap the entity tail index fell into
    # one commit earlier, in the same file, for the same reason.
    #
    # Measured on the board at 65 001 entities: matching cost 0.43 ms with no
    # mentions and 2.83 ms with 7 500 — the whole difference was this scan, on a
    # table that grows with the graph.
    #
    # Renamed and the old name dropped, because ``CREATE INDEX IF NOT EXISTS``
    # matches on the name: keeping it would leave every existing graph on the
    # two-column form, silently, forever.
    """
    DROP INDEX IF EXISTS idx_kg_mentions_alias
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kg_mentions_alias_entity
        ON kg_entity_mentions (space_id, alias, entity_id)
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
    s.adapter_name  AS adapter_name,
    -- Rendering needs to say *when the conversation was*, which is this and not
    -- ``valid_from``: a fact can start years before it is mentioned ("我 2015 年
    -- 搬到杭州"), and telling the model a 2015 provenance for something heard
    -- last week is worse than telling it nothing.
    s.recorded_at   AS recorded_at
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
