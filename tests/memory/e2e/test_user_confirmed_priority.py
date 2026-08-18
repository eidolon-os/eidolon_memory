"""Phase 5.2 e2e — user-confirmed fact lands verbatim + pins at recall top.

Edge under test (NATS-write → MCP-read contract, no LLM):

    NATS cmd `memory_intent` → agent_runner subscriber
      → process_command_message → explicit intent applier
      → chromadb drawer (source="user-confirmed", confidence=0.99)
    → MCP recall_context → recall_with_kg_fusion pins it ahead of
      cosine-ranked siblings in the same wing.

steward.mode=noop: the only write path here is the cmd subject, so chat
turns can't muddy the assertion. No LLM dependency — this is a pure
mechanism test.
"""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    mcp_tool_json,
    nats_publish_turn,
    nats_publish_user_confirm,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _list_values(session) -> list[str]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_list", {"limit": 1000, "include_private": True},
        )
    )
    if not isinstance(payload, dict):
        return []
    return [str(r.get("value", "")) for r in (payload.get("records") or [])]


async def _recall_values(session, context, *, query: str) -> list[str]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "context": context, "top_k": 5, "voice": False},
        )
    )
    if not isinstance(payload, dict):
        return []
    return [str(r.get("value", "")) for r in (payload.get("records") or [])]


async def _recall_records(session, context, *, query: str) -> list[dict]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "context": context, "top_k": 5, "voice": False},
        )
    )
    if not isinstance(payload, dict):
        return []
    return payload.get("records") or []


async def test_user_confirmed_lands_verbatim_and_pins_top(
    live_agent_runner, mcp_session
):
    """A user-confirmed fact survives verbatim and outranks chat-derived
    drawers about the same topic."""
    handle = live_agent_runner(
        user_id="e2e_userconfirm", steward_mode="noop",
    )
    ctx = e2e_actor_context(handle.user_id)

    # ─── seed some chat turns in the same wing (noop steward → these do NOT
    #     produce drawers; they just exercise the turn path) plus a couple of
    #     regular drawers via the agent. Since noop writes nothing, we rely on
    #     the user-confirm cmd for the actual data. To prove PINNING we also
    #     publish a second user-confirm + verify ordering against it.
    confirmed_text = "我喝乌龙茶不喝咖啡也不喝绿茶"
    await nats_publish_user_confirm(
        handle.nats_url, user_id=handle.user_id,
        text=confirmed_text, wing="Wing_Profile", memory_type="preference",
    )
    # A second, unrelated preference (also user-confirmed) to ensure multiple
    # land and the listing reflects both.
    await nats_publish_user_confirm(
        handle.nats_url, user_id=handle.user_id,
        text="我每天早上六点起床", wing="Wing_Profile", memory_type="preference",
    )

    async with mcp_session(handle.mcp_url) as session:
        # Wait until both user-confirmed drawers are visible.
        async def _both_landed(s) -> bool:
            vals = await _list_values(s)
            return any(confirmed_text == v for v in vals) and len(vals) >= 2

        assert await wait_for_visible(session, predicate=_both_landed, timeout_s=30), (
            "user-confirmed drawers did not appear via MCP list within 30s"
        )

        # ─── verbatim check: the exact string we sent is what landed ──
        vals = await _list_values(session)
        assert confirmed_text in vals, (
            f"verbatim text not found in listing; got {vals}"
        )

        # ─── recall returns the user-confirmed drawer, pinned at top ──
        # NOTE: mempalace's vector search drops custom metadata (incl.
        # ``source``) — the surviving signal is the ``room`` prefix
        # ``userconfirm:`` (see USER_CONFIRMED_ROOM_PREFIX), which is exactly
        # what the recall pin keys off. Assert against that, not ``source``.
        records = await _recall_records(session, ctx, query="我喝什么")
        assert records, "recall returned no records for '我喝什么'"
        top = records[0]
        top_room = str(top.get("metadata", {}).get("room", ""))
        assert top_room.startswith("userconfirm:"), (
            f"top recall record is not user-confirmed (room={top_room!r}); "
            f"metadata={top.get('metadata')}"
        )
        # And it should be the beverage fact (most relevant to the query).
        assert "乌龙茶" in str(top.get("value", "")), (
            f"expected beverage fact pinned at top, got {top.get('value')}"
        )


async def test_user_confirmed_outranks_chat_drawer_same_topic(
    live_agent_runner, mcp_session
):
    """With rules steward producing a chat-derived drawer AND a user-confirmed
    drawer in the same wing, the user-confirmed one pins ahead."""
    handle = live_agent_runner(
        user_id="e2e_userconfirm_rank", steward_mode="rules",
    )
    ctx = e2e_actor_context(handle.user_id)

    # 1) Chat turn → rules steward writes a normal drawer about tea.
    await nats_publish_turn(
        handle.nats_url, user_id=handle.user_id,
        user_text="我今天random喝了点茶感觉还行",
        assistant_text="嗯,听起来不错。",
        turn_id="uc-chat-1",
    )
    # 2) User explicitly confirms a stronger statement.
    await nats_publish_user_confirm(
        handle.nats_url, user_id=handle.user_id,
        text="我只喝乌龙茶,坚决不喝咖啡",
        wing="Wing_Life", memory_type="preference",
    )

    def _is_confirmed(record: dict) -> bool:
        # mempalace search drops custom metadata.source; the room prefix is
        # the durable signal (matches the recall pin's own logic).
        room = str(record.get("metadata", {}).get("room", ""))
        return room.startswith("userconfirm:")

    async with mcp_session(handle.mcp_url) as session:
        async def _confirmed_present(s) -> bool:
            return any(_is_confirmed(r) for r in await _recall_records(s, ctx, query="我喝什么茶"))

        assert await wait_for_visible(
            session, predicate=_confirmed_present, timeout_s=40,
        ), "user-confirmed drawer never surfaced in recall"

        records = await _recall_records(session, ctx, query="我喝什么茶")
        flags = [_is_confirmed(r) for r in records]
        # The first user-confirmed record must precede any non-confirmed one.
        first_confirmed = next((i for i, f in enumerate(flags) if f), None)
        first_other = next((i for i, f in enumerate(flags) if not f), None)
        assert first_confirmed is not None, f"no user-confirmed in recall: {flags}"
        if first_other is not None:
            assert first_confirmed < first_other, (
                f"user-confirmed not pinned ahead of chat drawer: {flags}"
            )
