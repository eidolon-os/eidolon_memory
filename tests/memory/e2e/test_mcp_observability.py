"""End-to-end coverage for the MCP observability surface.

Architectural edges asserted:

    R9  ``eidolon_memory_status`` → identity / wings / config introspection
        (every other tool consults the same settings object, so a broken
        status response is a leading indicator)

    R8  ``eidolon_memory_list`` honors ``include_private`` and ``limit``
        pagination — the contract the admin / IDE UIs rely on

Why one file: both tools read the same agent state without writing; testing
them together keeps spawn cost low and makes the read-side surface area
visible in one place.
"""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    load_companion_corpus,
    mcp_tool_json,
    nats_publish_turn,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _status(session) -> dict:
    payload = mcp_tool_json(await session.call_tool("eidolon_memory_status", {}))
    return payload if isinstance(payload, dict) else {}


async def _list(session, *, limit: int = 1000, include_private: bool = False) -> list[dict]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_list",
            {"limit": limit, "include_private": include_private},
        )
    )
    if not isinstance(payload, dict):
        return []
    return payload.get("records") or []


async def test_status_returns_identity_and_config(live_agent_runner, mcp_session):
    """R9: status surfaces user_id, palace_path, steward_mode, wings."""
    handle = live_agent_runner(
        user_id="e2e_status", port=19060, steward_mode="rules",
    )
    async with mcp_session(handle.mcp_url) as session:
        s = await _status(session)

        # Identity — the user_id this runner was spawned with.
        assert s.get("user_id") == handle.user_id, s
        # Steward selection — surfaced from settings exactly.
        assert s.get("steward_mode") == "rules", s
        # Palace path must point under the test palace root.
        assert handle.user_id in str(s.get("palace_path", "")), s
        # Wings must be a non-empty list of {id, ...} records (canonical wings).
        wings = s.get("wings") or []
        wing_ids = {w.get("id") for w in wings if isinstance(w, dict)}
        assert wing_ids, f"wings empty: {s}"
        # Privacy must be in the canonical wing set.
        assert "Wing_Privacy" in wing_ids, wing_ids
        # MCP transport identifier — guards against accidental stdio fallback.
        assert s.get("mcp_transport") == "streamable-http", s


async def test_list_paginates_and_filters_private(live_agent_runner, mcp_session):
    """R8: ``eidolon_memory_list`` honors ``limit`` and ``include_private``.

    Strategy:
      1. Drive 10 turns with the rules steward to populate drawers.
      2. ``list(limit=3)`` returns ≤ 3 records.
      3. ``list(limit=1000, include_private=False)`` ≤ ``include_private=True``.
         With rules steward there are no Wing_Privacy rows (only LLM steward
         routes there), so the two MUST be equal — but the contract guarantees
         False ≤ True, which is the structural invariant we assert.
    """
    handle = live_agent_runner(
        user_id="e2e_list", port=19061, steward_mode="rules",
    )
    corpus = load_companion_corpus()
    for entry in corpus[:10]:
        await nats_publish_turn(
            handle.nats_url,
            user_id=handle.user_id,
            user_text=entry["user_text"],
            assistant_text=entry["assistant_text"],
            turn_id=entry["turn_id"],
        )

    async with mcp_session(handle.mcp_url) as session:
        # Wait for >= 1 fragment so the listing assertions have signal.
        async def _has_rows(s) -> bool:
            return len(await _list(s, limit=1000)) >= 1

        assert await wait_for_visible(session, predicate=_has_rows, timeout_s=60), (
            "rules steward did not write any drawers from 10 turns"
        )

        # ─── limit honored ─────────────────────────────────────────────────
        capped = await _list(session, limit=3, include_private=False)
        assert len(capped) <= 3, f"limit ignored: {len(capped)} > 3"

        # ─── private filter is a non-increasing predicate ──────────────────
        without_private = await _list(session, limit=1000, include_private=False)
        with_private    = await _list(session, limit=1000, include_private=True)
        assert len(without_private) <= len(with_private), (
            f"include_private=False returned MORE rows ({len(without_private)}) "
            f"than include_private=True ({len(with_private)}) — filter inverted"
        )

        # ─── every returned row carries the expected envelope keys ────────
        for row in with_private[:5]:
            assert {"user_id", "key", "value", "metadata"}.issubset(row.keys()), (
                f"row missing canonical fields: {sorted(row.keys())}"
            )
