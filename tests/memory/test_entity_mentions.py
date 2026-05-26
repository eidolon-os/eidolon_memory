"""Phase 3 — unit tests for the entity_mentions schema + alias matching layer.

Scope:
  * ``LockedKnowledgeGraph._ensure_entity_mentions_schema`` migration is
    idempotent and runs on every spawn.
  * ``record_entity_mention`` is idempotent via the UNIQUE(entity_id, alias)
    constraint.
  * ``match_entities_for_query`` strategy 3 (alias reverse lookup) fires
    when canonical strategies miss, and never double-emits when canonical
    AND alias both match the same entity.

E2E (LLM-driven mention extraction) lives separately in
``tests/memory/e2e/test_entity_mention_resolution.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


# ─── Helpers (fresh palace per test) ───────────────────────────────────────


def _make_kg(tmp_path: Path):
    """Build a fresh LockedKG against a per-test sqlite file."""
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    inner = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    return LockedKnowledgeGraph(inner, asyncio.Lock())


async def _seed_entities(kg, names_with_types: list[tuple[str, str]]) -> None:
    """Insert raw entities (no triples needed) into the KG fixture."""
    def _insert() -> None:
        conn = kg._inner._conn()
        for name, etype in names_with_types:
            conn.execute(
                "INSERT OR IGNORE INTO entities(id, name, type, properties, created_at) "
                "VALUES (?, ?, ?, '{}', strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (name, name, etype),
            )
        conn.commit()
    async with kg._lock:
        await asyncio.to_thread(_insert)


# ─── Schema migration ──────────────────────────────────────────────────────


def test_schema_migration_creates_entity_mentions_table(tmp_path):
    """First spawn — table + both indexes exist."""
    kg = _make_kg(tmp_path)
    try:
        conn = kg._inner._conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "entity_mentions" in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='entity_mentions'"
        )}
        assert "idx_mentions_alias" in indexes
        assert "idx_mentions_entity" in indexes
    finally:
        kg.close()


def test_schema_migration_idempotent_on_re_spawn(tmp_path):
    """Constructing the wrapper twice against the same db is safe."""
    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph
    from mempalace.knowledge_graph import KnowledgeGraph

    db = str(tmp_path / "kg.sqlite3")
    a = LockedKnowledgeGraph(KnowledgeGraph(db_path=db), asyncio.Lock())
    a.close()
    # Second wrapper — would raise if CREATE were unguarded.
    b = LockedKnowledgeGraph(KnowledgeGraph(db_path=db), asyncio.Lock())
    b.close()


# ─── record_entity_mention ─────────────────────────────────────────────────


async def test_record_mention_persists_row(tmp_path):
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [("mother:张丽", "person")])
        await kg.record_entity_mention(
            entity_id="mother:张丽", alias="我妈",
            source="steward-llm", confidence=0.95,
        )
        rows = list(kg._inner._conn().execute(
            "SELECT entity_id, alias, source, confidence FROM entity_mentions"
        ))
        assert len(rows) == 1
        eid, alias, src, conf = rows[0]
        assert eid == "mother:张丽"
        assert alias == "我妈"
        assert src == "steward-llm"
        assert abs(conf - 0.95) < 1e-6
    finally:
        kg.close()


async def test_record_mention_idempotent_via_unique(tmp_path):
    """Writing the same (entity_id, alias) twice collapses to one row."""
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [("pet:铁锤", "pet")])
        for _ in range(3):
            await kg.record_entity_mention(
                entity_id="pet:铁锤", alias="我家狗",
                source="steward-llm", confidence=0.85,
            )
        count = kg._inner._conn().execute(
            "SELECT COUNT(*) FROM entity_mentions"
        ).fetchone()[0]
        assert count == 1
    finally:
        kg.close()


async def test_record_mention_deterministic_id_format(tmp_path):
    """id = `<entity_id>::<sha8(alias)>` — testable from the outside."""
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [("mother:张丽", "person")])
        await kg.record_entity_mention(
            entity_id="mother:张丽", alias="妈妈",
            source="steward-llm", confidence=0.95,
        )
        expected = f"mother:张丽::{hashlib.sha256('妈妈'.encode()).hexdigest()[:8]}"
        row = kg._inner._conn().execute(
            "SELECT id FROM entity_mentions"
        ).fetchone()
        assert row[0] == expected
    finally:
        kg.close()


async def test_record_mention_rejects_empty_inputs(tmp_path):
    kg = _make_kg(tmp_path)
    try:
        # No raise; both branches are no-ops by contract.
        await kg.record_entity_mention(entity_id="", alias="我妈", source="x")
        await kg.record_entity_mention(entity_id="mother", alias="", source="x")
        count = kg._inner._conn().execute(
            "SELECT COUNT(*) FROM entity_mentions"
        ).fetchone()[0]
        assert count == 0
    finally:
        kg.close()


# ─── match_entities_for_query: alias strategy ─────────────────────────────


async def test_match_alias_lookup_hits_canonical(tmp_path):
    """Canonical entity_id surfaces from a colloquial query via aliases."""
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [("mother:张丽", "person")])
        await kg.record_entity_mention(
            entity_id="mother:张丽", alias="我妈",
            source="steward-llm", confidence=0.95,
        )
        hits = await kg.match_entities_for_query("我妈这周怎么样", cap=3)
        assert hits == ["mother:张丽"]
    finally:
        kg.close()


async def test_match_alias_longest_first(tmp_path):
    """Longer aliases win — ``我老婆`` should match before bare ``老婆``."""
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [
            ("partner:王芳", "person"),
            ("hypothetical-mother-in-law", "person"),
        ])
        # Both aliases point at different entities, both substring-match
        # query "我老婆呢" — but "我老婆" is the longer, so partner:王芳 wins.
        await kg.record_entity_mention(
            entity_id="partner:王芳", alias="我老婆",
            source="steward-llm", confidence=0.95,
        )
        await kg.record_entity_mention(
            entity_id="hypothetical-mother-in-law", alias="老婆",
            source="steward-llm", confidence=0.50,
        )
        hits = await kg.match_entities_for_query("我老婆呢", cap=5)
        assert hits[0] == "partner:王芳"
    finally:
        kg.close()


async def test_match_canonical_beats_alias_no_double_emit(tmp_path):
    """If canonical strategy 1/2 already hit entity X, strategy 3 does NOT
    re-emit X via one of X's aliases."""
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [("mother:张丽", "person")])
        await kg.record_entity_mention(
            entity_id="mother:张丽", alias="妈妈",
            source="steward-llm", confidence=0.95,
        )
        # Query contains BOTH the canonical prefix-stripped "张丽" AND alias "妈妈".
        hits = await kg.match_entities_for_query("妈妈 张丽 怎么样", cap=5)
        assert hits.count("mother:张丽") == 1
    finally:
        kg.close()


async def test_match_alias_respects_cap(tmp_path):
    """Cap stops emitting after N entities even when many aliases match."""
    kg = _make_kg(tmp_path)
    try:
        names = [(f"person:{i}", "person") for i in range(10)]
        await _seed_entities(kg, names)
        for i in range(10):
            await kg.record_entity_mention(
                entity_id=f"person:{i}", alias=f"alias{i}",
                source="steward-llm", confidence=0.85,
            )
        hits = await kg.match_entities_for_query(
            " ".join(f"alias{i}" for i in range(10)), cap=3
        )
        assert len(hits) == 3
    finally:
        kg.close()


async def test_match_no_aliases_still_works(tmp_path):
    """Backwards-compat: a palace with zero mentions falls back to canonical."""
    kg = _make_kg(tmp_path)
    try:
        await _seed_entities(kg, [("pet:铁锤", "pet")])
        hits = await kg.match_entities_for_query("铁锤是什么品种", cap=3)
        # Strategy 2 (prefix-stripped tail) still hits.
        assert hits == ["pet:铁锤"]
    finally:
        kg.close()


# ─── Replay safety ─────────────────────────────────────────────────────────


def test_steward_decision_without_mentions_field_replay_safe():
    """An older JetStream payload (pre-Phase-3) lacks ``mentions`` —
    pydantic default ``[]`` must kick in without ValidationError.
    """
    from eidolon.memory.domain.steward import StewardDecision

    legacy_payload = {
        "should_write": False,
        "reason": "ok",
        "fragments": [],
        "triples": [],
        "invalidations": [],
        "privacy_actions": [],
        # NO 'mentions' key — replay scenario
    }
    decision = StewardDecision.model_validate(legacy_payload)
    assert decision.mentions == []


def test_entity_mention_confidence_clamped():
    """``EntityMention`` enforces 0 ≤ confidence ≤ 1."""
    from eidolon.memory.domain.steward import EntityMention

    with pytest.raises(Exception):  # pydantic ValidationError
        EntityMention(entity_id="x", alias="y", confidence=1.5)
    with pytest.raises(Exception):
        EntityMention(entity_id="x", alias="y", confidence=-0.1)
    # Boundary values OK
    assert EntityMention(entity_id="x", alias="y", confidence=0.0).confidence == 0.0
    assert EntityMention(entity_id="x", alias="y", confidence=1.0).confidence == 1.0
