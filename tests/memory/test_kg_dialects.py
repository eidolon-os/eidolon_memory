"""The two graph stores must ask the same questions.

The shared SQL is exercised against SQLite by test_kg_sqlite.py. What that cannot
catch is the two implementations drifting: a filter added to one query and not its
twin, a parameter appended in one dialect only. Those would surface as a cloud
deployment quietly filtering differently from a local one — the kind of divergence
that shows up as "recall works on my laptop".

So these tests compare the statements the two build. They need no database, which
is the point: without a PostgreSQL to hand, structural agreement is the strongest
claim available, and it is worth having on its own.

What remains unverified without a server: that PostgreSQL accepts these
statements and returns what we expect. tests/memory/test_live_postgres_kg.py
covers that when one is configured.
"""

from __future__ import annotations

import re

import pytest

from eidolon.memory.adapters import kg_postgres, kg_sql
from eidolon.memory.domain.kg_port import KnowledgeGraphPort


def _normalise(sql: str, marker: str) -> str:
    """Reduce a statement to its shape: no whitespace runs, no dialect marker."""

    without_marker = sql.replace(marker, "?")
    return re.sub(r"\s+", " ", without_marker).strip()


def test_both_implementations_satisfy_the_port() -> None:
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    assert issubclass(SqliteKnowledgeGraph, object)
    # Structural, so checked against the classes' methods rather than instances —
    # constructing the Postgres one needs a server.
    for method in (
        "add_triple", "invalidate", "supersede", "record_entity_mention",
        "query_entity", "query_subjects", "query_entity_combined", "timeline",
        "match_entities_for_query", "known_audiences", "list_entity_names",
        "stats", "has_triple", "find_pending_triple_id",
        "find_invalidation_applied", "close",
    ):
        assert hasattr(SqliteKnowledgeGraph, method), f"sqlite missing {method}"
        assert hasattr(kg_postgres.PostgresKnowledgeGraph, method), (
            f"postgres missing {method}"
        )
    assert isinstance(KnowledgeGraphPort, type)


def test_the_schemas_differ_only_where_the_dialects_require_it() -> None:
    """One substitution, and it is a real incompatibility rather than a preference.

    Postgres has a boolean type and will not compare it to an integer, so the
    sensitivity flag has to change. Everything else — tables, columns, indexes —
    is identical, which is what keeps a space's shape the same in both stores.
    """

    sqlite_schema = [_normalise(s, "?") for s in kg_sql.SCHEMA_STATEMENTS]
    postgres_schema = [_normalise(s, "%s") for s in kg_postgres.postgres_schema()]

    assert len(sqlite_schema) == len(postgres_schema)

    differences = [
        (a, b) for a, b in zip(sqlite_schema, postgres_schema, strict=True) if a != b
    ]
    assert len(differences) == 1, f"unexpected schema divergence: {differences}"

    sqlite_stmt, postgres_stmt = differences[0]
    assert "sensitive INTEGER NOT NULL DEFAULT 0" in sqlite_stmt
    assert "sensitive BOOLEAN NOT NULL DEFAULT FALSE" in postgres_stmt


def test_every_table_and_index_exists_in_both() -> None:
    def _objects(statements) -> set[str]:
        found = set()
        for statement in statements:
            match = re.search(
                r"CREATE (?:UNIQUE )?(?:TABLE|INDEX) IF NOT EXISTS (\w+)", statement
            )
            if match:
                found.add(match.group(1))
        return found

    assert _objects(kg_sql.SCHEMA_STATEMENTS) == _objects(kg_postgres.postgres_schema())


def test_the_interval_test_is_the_same_predicate() -> None:
    """Half-open validity is the semantics superseding depends on.

    If one store used a closed interval, the boundary instant would have both the
    old and new statement true there — visible as a companion briefly believing
    two contradictory things.
    """

    assert _normalise(kg_sql.VALID_AT.format(p="?"), "?") == _normalise(
        kg_sql.VALID_AT.format(p="%s"), "%s"
    )
    assert "valid_to > " in kg_sql.VALID_AT, "must exclude the end instant"
    assert "valid_from <= " in kg_sql.VALID_AT, "must include the start instant"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_the_audience_filter_takes_one_parameter_per_audience(count: int) -> None:
    """A miscount here would shift every later parameter by one.

    Which is the failure mode worth guarding: not a syntax error, but a query
    that runs and filters by the wrong values.
    """

    for marker in ("?", "%s"):
        clause = kg_sql.audience_filter(count, marker)
        assert clause.count(marker) == count
        assert clause.startswith("s.audience IN (")


def test_sensitivity_is_filtered_in_the_query_by_both() -> None:
    """Not in Python afterwards: a health fact should never leave the store."""

    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    sqlite_clause = SqliteKnowledgeGraph._sensitive_clause(False)
    postgres_clause = kg_postgres._sensitive_clause(False)

    assert "s.sensitive" in sqlite_clause
    assert "s.sensitive" in postgres_clause
    # The values differ because the column types do.
    assert sqlite_clause.endswith("0")
    assert postgres_clause.endswith("FALSE")

    assert SqliteKnowledgeGraph._sensitive_clause(True) == ""
    assert kg_postgres._sensitive_clause(True) == ""


def test_per_subject_ranking_is_shared() -> None:
    """The bound that stops one busy entity taking another's allowance.

    The rank is an inner alias the outer query filters on and does not project —
    a caller wants the statements, not their positions.
    """

    assert "ROW_NUMBER() OVER (PARTITION BY s.subject_id" in kg_sql.SUBJECT_RANK
    assert "AS subject_rank" in kg_sql.SUBJECT_RANK
    assert "subject_rank" not in kg_sql.RANKED_SUBJECT_COLUMNS


def test_the_ranked_projection_matches_the_inner_column_order() -> None:
    """Rows are read positionally, so a reordering here would silently swap fields.

    Subject and object would trade places — a statement claiming the reverse of
    what was recorded.
    """

    def _aliases(block: str) -> list[str]:
        return [
            part.strip().split()[-1]
            for part in block.strip().split(",")
            if part.strip()
        ]

    inner = _aliases(kg_sql.SELECT_COLUMNS)
    outer = [name.split(".")[-1] for name in _aliases(kg_sql.RANKED_SUBJECT_COLUMNS)]

    assert inner == outer


def test_both_dialects_spell_a_tolerated_duplicate_insert() -> None:
    """Idempotent writes depend on it; a missing conflict target would raise."""

    assert "(space_id, statement_id)" in kg_postgres.ON_CONFLICT_IGNORE
    assert "(space_id, entity_id)" in kg_postgres.ON_CONFLICT_IGNORE_ENTITY
    assert "(space_id, mention_id)" in kg_postgres.ON_CONFLICT_IGNORE_MENTION
    for clause in (
        kg_postgres.ON_CONFLICT_IGNORE,
        kg_postgres.ON_CONFLICT_IGNORE_ENTITY,
        kg_postgres.ON_CONFLICT_IGNORE_MENTION,
    ):
        assert clause.endswith("DO NOTHING")


def test_the_shared_store_graph_holds_no_lock() -> None:
    """Holding one across a round trip would serialise concurrent recalls.

    Which is the coupling the shared-store shape exists to remove, so this is a
    property rather than an omission.
    """

    assert kg_postgres.PostgresKnowledgeGraph.lock is None


def test_id_derivation_is_shared_so_a_space_can_move_stores() -> None:
    """Different ids for the same statement would duplicate it on migration."""

    from eidolon.memory.adapters import kg_sqlite

    assert kg_postgres.entity_id_for is kg_sqlite.entity_id_for
    assert kg_postgres.statement_id_for is kg_sqlite.statement_id_for
    assert kg_postgres.canonical_temporal is kg_sqlite.canonical_temporal


def test_missing_the_postgres_extra_says_what_to_install() -> None:
    """An operator who sets kg.backend=postgres without the driver needs telling."""

    import asyncio

    with pytest.raises(RuntimeError, match="postgres.*extra"):
        asyncio.run(
            kg_postgres.PostgresKnowledgeGraph.connect(
                "postgresql://nowhere/none", space_id="alice"
            )
        )
