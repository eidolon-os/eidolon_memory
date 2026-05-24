"""End-to-end coverage for the KG admin pipeline.

Architectural edges asserted in one cohesive scenario:

    W7  NATS ``agent.memory.cmd.<uid>`` → ``process_command_message`` →
        ``LockedKnowledgeGraph.add_triple``  (admin / agent write channel)

    R4  ``recall_with_kg_fusion`` → ``match_entities_for_query`` →
        ``query_entity_combined`` → triples surface in
        ``eidolon_memory_recall_context``

    R10 ``eidolon_memory_kg_query_entity`` / ``kg_timeline`` / ``kg_stats``
        all return data consistent with what W7 wrote

Contract reminder: writes must go through NATS, reads through MCP. No
in-process backdoor (no direct ``kg.add_triple`` from the test) — that's
what the W7 edge is for.
"""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_kg_add_triple,
    nats_publish_kg_invalidate,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


# ─── ground truth: triples we'll inject via the cmd subject ────────────────
#
# Chosen so the recall query and the KG entity-routing both naturally hit
# the canonical entity names (no prefix-stripping needed for these subjects).
_TRIPLES: tuple[dict, ...] = (
    {"subject": "self",    "predicate": "likes",      "obj": "oolong", "confidence": 0.95},
    {"subject": "self",    "predicate": "likes",      "obj": "tea",    "confidence": 0.90},
    {"subject": "self",    "predicate": "dislikes",   "obj": "coffee", "confidence": 0.85},
    {"subject": "self",    "predicate": "lives_in",   "obj": "Beijing","confidence": 0.99},
    {"subject": "alice",   "predicate": "works_at",   "obj": "Acme",   "confidence": 0.80},
)


async def _kg_stats(session) -> dict:
    """Schema: {entities, triples_total, triples_active, triples_invalidated}."""
    payload = mcp_tool_json(await session.call_tool("eidolon_memory_kg_stats", {}))
    return payload if isinstance(payload, dict) else {}


async def _kg_query_entity(session, *, name: str) -> list[dict]:
    """Schema: {entity, as_of, direction, triples: [{subject, predicate, object, ...}]}."""
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_kg_query_entity",
            {"name": name, "include_sensitive": False},
        )
    )
    if not isinstance(payload, dict):
        return []
    return payload.get("triples") or []


async def _kg_timeline(session, *, entity_name: str | None = None, limit: int = 50) -> list[dict]:
    """Schema: {entity_name, since, until, events: [...]}."""
    args: dict = {"limit": limit}
    if entity_name:
        args["entity_name"] = entity_name
    payload = mcp_tool_json(
        await session.call_tool("eidolon_memory_kg_timeline", args)
    )
    if not isinstance(payload, dict):
        return []
    return payload.get("events") or []


async def _recall_context(session, *, query: str, top_k: int = 5) -> dict:
    """Schema: {context: str, kg_triples: [...], records: [...]}."""
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "top_k": top_k, "voice": False},
        )
    )
    return payload if isinstance(payload, dict) else {}


async def test_kg_admin_cmd_pipeline_full_roundtrip(live_agent_runner, mcp_session):
    """W7 + R4 + R10 in a single coherent scenario.

    1. Spawn a clean agent (steward.mode=noop so chat-derived triples cannot
       interfere; only the cmd subject writes KG).
    2. Publish 5 ``KgAddTripleCommand`` messages via NATS.
    3. Wait until ``kg_stats`` reflects all 5 triples (R10 → durable write).
    4. ``kg_query_entity('self')`` returns the 4 self-anchored triples (R10).
    5. ``kg_timeline()`` returns all 5 in chronological-ish order (R10).
    6. ``recall_context(query='self likes tea')`` returns the KG fusion
       payload, and the rendered context references at least one of the
       written objects (R4 wire-up: KG → recall envelope).
    """
    handle = live_agent_runner(
        user_id="e2e_kg_admin", port=19050, steward_mode="noop",
    )

    # ─── W7: NATS cmd subject → KG write ──────────────────────────────────
    for t in _TRIPLES:
        await nats_publish_kg_add_triple(
            handle.nats_url,
            user_id=handle.user_id,
            subject=t["subject"], predicate=t["predicate"],
            obj=t["obj"], confidence=t["confidence"],
        )

    async with mcp_session(handle.mcp_url) as session:
        # ─── R10: wait for kg_stats to reflect the writes ─────────────────
        async def _five_triples_landed(s) -> bool:
            stats = await _kg_stats(s)
            return int(stats.get("triples_total") or 0) >= len(_TRIPLES)

        assert await wait_for_visible(session, predicate=_five_triples_landed, timeout_s=30), (
            "KG cmd writes did not appear in kg_stats within 30s — "
            "either the cmd subscriber is broken or kg_stats is stale"
        )
        # Snapshot for diagnostics + assertion.
        stats = await _kg_stats(session)
        assert stats.get("triples_active", 0) >= len(_TRIPLES), (
            f"kg_stats missing active triples: {stats}"
        )

        # ─── R10: kg_query_entity for 'self' returns ≥ 4 ──────────────────
        self_triples = await _kg_query_entity(session, name="self")
        self_objects = {t.get("object") for t in self_triples}
        assert {"oolong", "tea", "coffee", "Beijing"}.issubset(self_objects), (
            f"kg_query_entity('self') missing expected objects; got {self_objects}"
        )
        assert "Acme" not in self_objects, (
            "alice:works_at:Acme leaked into entity='self' result"
        )

        # ─── R10: kg_timeline returns ≥ 5 across all entities ─────────────
        timeline = await _kg_timeline(session, entity_name=None, limit=50)
        all_objects = {t.get("object") for t in timeline}
        assert {"oolong", "tea", "coffee", "Beijing", "Acme"}.issubset(all_objects), (
            f"kg_timeline missing objects; got {all_objects}"
        )

        # ─── R10: kg_query_entity for 'alice' returns alice's triple ──────
        alice_triples = await _kg_query_entity(session, name="alice")
        assert any(t.get("object") == "Acme" for t in alice_triples), (
            f"kg_query_entity('alice') missing Acme; got {alice_triples}"
        )

        # ─── R4: KG fusion surfaces written triples in recall_context ─────
        ctx = await _recall_context(session, query="self likes tea", top_k=5)
        # Two complementary signals — both must hold:
        # (a) Structured ``kg_triples`` list contains the fact.
        kg_block = ctx.get("kg_triples") or []
        kg_objs = {t.get("object") for t in kg_block if isinstance(t, dict)}
        assert kg_objs & {"oolong", "tea"}, (
            f"recall_context.kg_triples did not surface KG fusion; got {kg_block}"
        )
        # (b) Rendered ``context`` string mentions at least one written object
        #     (transcribed by ``transcribe_triples`` into Chinese narrative).
        rendered = str(ctx.get("context") or "")
        assert any(o in rendered for o in ("tea", "oolong", "茶")), (
            f"rendered context omitted KG facts; got: {rendered[:300]}…"
        )


async def test_kg_invalidate_cmd_closes_triple(live_agent_runner, mcp_session):
    """W7 second command kind: ``KgInvalidateCommand`` flips a triple's
    ``valid_to`` from null to now-ish, removing it from ``triples_active``
    while leaving ``triples_total`` unchanged.
    """
    handle = live_agent_runner(
        user_id="e2e_kg_invalidate", port=19051, steward_mode="noop",
    )
    # Seed one triple via the add channel first.
    await nats_publish_kg_add_triple(
        handle.nats_url, user_id=handle.user_id,
        subject="self", predicate="lives_in", obj="Beijing", confidence=0.99,
    )

    async with mcp_session(handle.mcp_url) as session:
        async def _seeded(s) -> bool:
            stats = await _kg_stats(s)
            return int(stats.get("triples_active") or 0) >= 1

        assert await wait_for_visible(session, predicate=_seeded, timeout_s=20)
        before = await _kg_stats(session)
        assert before["triples_active"] == 1, before
        assert before["triples_invalidated"] == 0, before

        # Now invalidate it.
        await nats_publish_kg_invalidate(
            handle.nats_url, user_id=handle.user_id,
            subject="self", predicate="lives_in", obj="Beijing",
        )

        async def _invalidated(s) -> bool:
            stats = await _kg_stats(s)
            return int(stats.get("triples_invalidated") or 0) >= 1

        assert await wait_for_visible(session, predicate=_invalidated, timeout_s=20), (
            "KG invalidate cmd did not flip triples_invalidated within 20s"
        )
        after = await _kg_stats(session)
        # Total is monotone (bi-temporal: invalidated rows are not deleted).
        assert after["triples_total"] == before["triples_total"], (
            f"triples_total moved unexpectedly: {before} → {after}"
        )
        assert after["triples_active"] == 0, after
        assert after["triples_invalidated"] == 1, after
