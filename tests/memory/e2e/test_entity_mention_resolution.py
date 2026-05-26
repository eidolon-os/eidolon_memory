"""Phase 3 e2e — LLM-driven entity mention extraction + alias-resolved recall.

The bridge this test asserts:

    NATS turn publish (with kinship/category/pronoun language)
      → agent_runner subscriber
      → turn_processor (LLM steward extracts triples + mentions)
      → LockedKG writes triples + entity_mentions rows (anti-hallucination
        guard rejects mentions whose entity_id wasn't in the same turn's
        triples)
      → MCP recall_context(query="我妈最近怎样")
      → LockedKG.match_entities_for_query strategy 3 (alias reverse lookup)
        surfaces canonical entity → fusion returns its triples → renderer
        emits 知识图谱事实 section in the MCP envelope

Why this is the *only* e2e in the suite that requires a real LLM:
  the entire point of mentions is "natural-language verbatim alias" → only
  an LLM can decide whether ``我妈`` should map to ``mother:张丽`` from
  the surrounding conversation. Strategies 1+2 (canonical / prefix-stripped)
  are fully covered by the existing kg_admin_pipeline e2e + this layer's
  unit tests.

Markers:
  - ``@pytest.mark.e2e`` — gated; run via ``-m e2e``
  - ``@pytest.mark.llm`` — additionally gated; requires
    ``EIDOLON_MEMORY_LLM_API_KEY`` set + a reachable ``llm.base_url``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from tests.memory.e2e.conftest import (
    load_companion_corpus,
    mcp_tool_json,
    nats_publish_turn,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.llm]


# Aliases we expect LLM steward to extract from the relationship-heavy slice
# of the companion corpus. The value is a *set* of acceptable entity_id
# substrings — LLMs vary on prefix taxonomy ("partner:" vs "wife:" vs
# "spouse:"), all of which encode the same canonical relation; we score
# on semantic category, not on our preferred prefix vocabulary.
_EXPECTED_ALIAS_HITS: dict[str, set[str]] = {
    "我妈":   {"mother", "mom"},
    "妈妈":   {"mother", "mom"},
    "我老婆": {"partner", "wife", "spouse"},
    "我家狗": {"pet", "dog"},
    "铁锤":   {"pet", "dog"},
}


async def _publish_relationship_subset(handle, corpus: list[dict]) -> list[str]:
    """Publish the relationship/pet/family slice (≥12 turns)."""
    selected_ids: list[str] = []
    for entry in corpus:
        wing = entry.get("wing_hint", "")
        # Relationship + Pet wings carry the alias-rich turns.
        if wing in ("Wing_Relationship", "Wing_Pet", "Wing_Life", "Wing_Health"):
            await nats_publish_turn(
                handle.nats_url,
                user_id=handle.user_id,
                user_text=entry["user_text"],
                assistant_text=entry["assistant_text"],
                turn_id=entry["turn_id"],
            )
            selected_ids.append(entry["turn_id"])
    return selected_ids


def _read_mentions_sql(palace_dir: Path) -> list[tuple[str, str]]:
    """Read entity_mentions rows directly from the KG sqlite (truth check).

    e2e contract allows read-only sqlite probing for assertion — writes
    must still go through NATS. See tests/memory/e2e/conftest.py header.
    """
    kg_db = palace_dir / "knowledge_graph.sqlite3"
    if not kg_db.is_file():
        return []
    conn = sqlite3.connect(str(kg_db))
    try:
        rows = list(conn.execute(
            "SELECT entity_id, alias FROM entity_mentions"
        ))
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return rows


@pytest.fixture
def _require_llm():
    """Skip if no LLM API key is available."""
    if not os.environ.get("EIDOLON_MEMORY_LLM_API_KEY", "").strip():
        # Try config/.env as a fallback so local devs don't need to export.
        env_path = Path(__file__).resolve().parents[3] / "config" / ".env"
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                if line.startswith("EIDOLON_MEMORY_LLM_API_KEY=") and "=" in line:
                    key, val = line.split("=", 1)
                    if val.strip():
                        os.environ[key] = val.strip()
                        break
        if not os.environ.get("EIDOLON_MEMORY_LLM_API_KEY", "").strip():
            pytest.skip("EIDOLON_MEMORY_LLM_API_KEY not set; LLM e2e gated")


async def test_llm_extracts_mentions_and_alias_query_routes_via_kg(
    _require_llm, live_agent_runner, mcp_session
):
    """The full Phase 3 contract on a real LLM:

    1. Publish corpus subset (relationship + pet wings, ≥12 turns).
    2. Wait until KG has ≥ N triples (proxy for "steward ran").
    3. Inspect entity_mentions SQL — must contain at least the kinship aliases
       we labeled in the corpus ground truth.
    4. Per ``expected_recall_queries`` with ``should_match=true``, the MCP
       ``recall_context`` envelope's ``kg_triples`` must surface the
       canonical entity (mother / partner / pet) — proving the alias →
       canonical pipeline works end-to-end.
    """
    corpus = load_companion_corpus()
    handle = live_agent_runner(
        user_id="e2e_p3_mention", port=19080, steward_mode="llm",
    )

    selected_ids = await _publish_relationship_subset(handle, corpus)
    assert len(selected_ids) >= 12, (
        f"corpus slice too small: {len(selected_ids)} (need ≥12)"
    )

    async with mcp_session(handle.mcp_url) as session:
        # Wait for the LLM steward to produce triples — be patient: LLM
        # round-trip adds latency. KG stats is the most direct signal.
        async def _kg_warm(s) -> bool:
            payload = mcp_tool_json(
                await s.call_tool("eidolon_memory_kg_stats", {})
            )
            if not isinstance(payload, dict):
                return False
            return int(payload.get("triples_total") or 0) >= 8

        ok = await wait_for_visible(session, predicate=_kg_warm, timeout_s=180)
        if not ok:
            stats = mcp_tool_json(
                await session.call_tool("eidolon_memory_kg_stats", {})
            )
            pytest.fail(
                f"LLM steward did not produce ≥8 KG triples in 180s; "
                f"final kg_stats={stats}"
            )

        # ── Truth check: aliases written to SQL ──
        mentions = _read_mentions_sql(handle.palace_dir)
        assert mentions, (
            "entity_mentions table is empty — LLM never emitted mentions or "
            "anti-hallucination guard rejected every one"
        )

        alias_to_entity: dict[str, str] = {}
        for entity_id, alias in mentions:
            alias_to_entity[alias] = entity_id

        # Soft category assertion: at least 1 of the expected alias→category
        # pairs landed. LLM coverage on a 12-turn slice has variance, and
        # the *primary* assertion (alias-query routing below) is the real
        # gate — this one's a sanity check that LLM is producing structured
        # mentions at all, not a quality bar.
        hits = 0
        matched_pairs: list[tuple[str, str]] = []
        for alias_sub, accepted_entity_subs in _EXPECTED_ALIAS_HITS.items():
            for alias, entity in alias_to_entity.items():
                entity_lower = entity.lower()
                if alias_sub in alias and any(s in entity_lower for s in accepted_entity_subs):
                    hits += 1
                    matched_pairs.append((alias, entity))
                    break
        print(
            f"\n[Phase 3 e2e] mentions written: {len(mentions)} rows; "
            f"category hits: {hits}/{len(_EXPECTED_ALIAS_HITS)}\n"
            f"  alias→entity map: {alias_to_entity}\n"
            f"  matched pairs:    {matched_pairs}\n"
        )
        assert hits >= 1, (
            "no alias→category pair matched; LLM either failed mention "
            f"extraction entirely or used unexpected taxonomy. "
            f"Got: {alias_to_entity}"
        )

        # ── Functional: alias queries route to canonical entities ──
        # Pick recall queries that the corpus marks should_match=true AND
        # whose phrasing leans on alias language. Each row gives a *set*
        # of acceptable entity-id substrings (taxonomy-tolerant — see
        # ``_EXPECTED_ALIAS_HITS`` rationale).
        alias_queries: list[tuple[str, set[str]]] = [
            ("我妈最近怎样",   {"mother", "mom"}),
            ("我老婆呢",       {"partner", "wife", "spouse"}),
            ("我家狗多大",     {"pet", "dog"}),
        ]
        routed_count = 0
        routed_detail: list[tuple[str, str]] = []
        for q, accepted in alias_queries:
            ctx = mcp_tool_json(await session.call_tool(
                "eidolon_memory_recall_context",
                {"query": q, "top_k": 5, "voice": False},
            ))
            if not isinstance(ctx, dict):
                continue
            kg_triples = ctx.get("kg_triples") or []
            for t in kg_triples:
                if not isinstance(t, dict):
                    continue
                blob = f"{t.get('subject','')} {t.get('object','')}".lower()
                if any(s in blob for s in accepted):
                    routed_count += 1
                    routed_detail.append((q, blob[:80]))
                    break
        # At minimum, 1/3 alias queries must route — proves the alias→canonical
        # pipeline works end-to-end (LLM quality on a 12-turn corpus has variance).
        assert routed_count >= 1, (
            f"alias→canonical routing failed for ALL queries; "
            f"alias_to_entity={alias_to_entity}, mentions_rows={len(mentions)}"
        )
        print(
            f"[Phase 3 e2e] alias-query routing: {routed_count}/{len(alias_queries)} hit\n"
            f"  detail: {routed_detail}\n"
        )
