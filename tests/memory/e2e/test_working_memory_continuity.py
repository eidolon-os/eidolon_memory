"""Phase 2 e2e — working-memory short-term continuity.

End-to-end edge:

    NATS turn publish → agent_runner subscriber → turn_processor.process_turn_message
        → backend.working_memory.append (in-memory deque)
    → MCP recall_context → recall_with_kg_fusion → working_memory snapshot
        → renderer ``[最近对话]`` section → MCP envelope

steward.mode=noop so the only path under test is the ring. Long-term
memory (chromadb / KG) is unaffected; restart wipes the ring while
chroma + KG persist (covered by the existing restart_hygiene test).
"""

from __future__ import annotations

import asyncio

import pytest

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    load_companion_corpus,
    mcp_tool_json,
    nats_publish_turn,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _recall(session, context, *, query: str = "刚才说什么") -> dict:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "context": context, "top_k": 5, "voice": False},
        )
    )
    return payload if isinstance(payload, dict) else {}


async def test_working_memory_returns_latest_10_verbatim(
    live_agent_runner, mcp_session
):
    """30 turns in → MCP returns the most recent 10 verbatim, oldest-first."""
    corpus = load_companion_corpus()
    # Use 30 turns so we comfortably exceed the default maxlen=10.
    PUBLISH_N = 30
    EXPECTED_MAXLEN = 10
    assert len(corpus) >= PUBLISH_N

    handle = live_agent_runner(
        user_id="e2e_wm", port=19070, steward_mode="noop",
    )
    # Recall context must match the write side (device_id + session_id) so the
    # working-memory ring snapshot for this device/session surfaces.
    ctx = e2e_actor_context(handle.user_id)

    # ─── publish in corpus order; ring must keep the LATEST 10 ─────────────
    for entry in corpus[:PUBLISH_N]:
        await nats_publish_turn(
            handle.nats_url,
            user_id=handle.user_id,
            user_text=entry["user_text"],
            assistant_text=entry["assistant_text"],
            turn_id=entry["turn_id"],
        )

    expected_recent_ids = [e["turn_id"] for e in corpus[PUBLISH_N - EXPECTED_MAXLEN: PUBLISH_N]]
    expected_recent_user_texts = [
        e["user_text"] for e in corpus[PUBLISH_N - EXPECTED_MAXLEN: PUBLISH_N]
    ]

    async with mcp_session(handle.mcp_url) as session:
        # Wait until the ring is fully primed: snapshot length must equal maxlen
        # AND the freshest expected id must be present.
        async def _primed(s) -> bool:
            r = await _recall(s, ctx)
            wm = r.get("working_memory") or []
            if len(wm) < EXPECTED_MAXLEN:
                return False
            ids = {t.get("turn_id") for t in wm}
            return expected_recent_ids[-1] in ids

        assert await wait_for_visible(session, predicate=_primed, timeout_s=60), (
            "working memory did not reach maxlen=10 with newest turn after 30 publishes"
        )

        result = await _recall(session, ctx)
        wm = result.get("working_memory") or []

        # ─── structural: exactly 10 entries, all from the most-recent slice ─
        assert len(wm) == EXPECTED_MAXLEN, (
            f"expected {EXPECTED_MAXLEN} turns in working_memory, got {len(wm)}"
        )
        returned_ids = [t.get("turn_id") for t in wm]
        assert returned_ids == expected_recent_ids, (
            f"working_memory order mismatch\n"
            f"  expected: {expected_recent_ids}\n"
            f"  got:      {returned_ids}"
        )

        # ─── verbatim: every recent user_text appears unchanged in the JSON ─
        for expected_text in expected_recent_user_texts:
            assert any(t.get("user_text") == expected_text for t in wm), (
                f"working_memory missing verbatim user_text: {expected_text!r}"
            )

        # ─── renderer: [最近对话] leads the context block ─────────────────
        ctx = str(result.get("context") or "")
        assert ctx.startswith("[最近对话]"), (
            f"context did not lead with [最近对话], got:\n{ctx[:300]}"
        )
        # Renderer caps at 5 turns × 2 lines (user + assistant). The most
        # recent corpus user_text MUST appear in the rendered block.
        latest_user_text = expected_recent_user_texts[-1]
        # Renderer may truncate at 200 chars; use a robust short prefix.
        prefix = latest_user_text[:30]
        assert prefix in ctx, (
            f"rendered context missing latest user_text prefix {prefix!r}\n{ctx[:500]}"
        )


async def test_working_memory_cleared_after_agent_restart(
    live_agent_runner, mcp_session
):
    """In-memory ring is wiped on restart; chromadb / KG (covered elsewhere)
    persist. Verifies the design choice: short-term continuity is process-local.
    """
    h1 = live_agent_runner(
        user_id="e2e_wm_restart_a", port=19071, steward_mode="noop",
    )
    ctx1 = e2e_actor_context(h1.user_id)
    # Publish a few turns.
    for entry in load_companion_corpus()[:5]:
        await nats_publish_turn(
            h1.nats_url,
            user_id=h1.user_id,
            user_text=entry["user_text"],
            assistant_text=entry["assistant_text"],
            turn_id=entry["turn_id"],
        )

    async with mcp_session(h1.mcp_url) as s:
        async def _has_some_wm(sess) -> bool:
            r = await _recall(sess, ctx1)
            return len(r.get("working_memory") or []) >= 1

        assert await wait_for_visible(s, predicate=_has_some_wm, timeout_s=20), (
            "first agent didn't populate working memory"
        )

    h1.kill()
    # Wait for port release + queue drain.
    await asyncio.sleep(2)

    # Spawn a fresh agent — NEW palace (fixture wipes by user_id), no shared
    # state with h1. The ring MUST be empty.
    h2 = live_agent_runner(
        user_id="e2e_wm_restart_b", port=19072, steward_mode="noop",
    )
    ctx2 = e2e_actor_context(h2.user_id)
    async with mcp_session(h2.mcp_url) as s:
        result = await _recall(s, ctx2)
        wm = result.get("working_memory") or []
        assert wm == [], f"new agent inherited working memory from prior run: {wm}"
