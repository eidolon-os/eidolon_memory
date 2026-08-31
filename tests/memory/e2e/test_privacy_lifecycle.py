"""Privacy lifecycle E2E through NATS writes and MCP reads only."""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    mcp_tool_json,
    nats_publish_assertion,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _list_records(session) -> list[dict]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_list",
            {"limit": 1000, "include_private": True},
        )
    )
    return list((payload or {}).get("records") or []) if isinstance(payload, dict) else []


async def _recall_values(session, context, query: str) -> list[str]:
    payload = mcp_tool_json(
        await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "context": context, "top_k": 5, "voice": False},
        )
    )
    if not isinstance(payload, dict):
        return []
    return [str(record.get("value") or "") for record in payload.get("records") or []]


async def test_archive_then_delete_respects_read_write_protocols(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_privacy_lifecycle",
        steward_mode="noop",
    )
    context = e2e_actor_context(handle.user_id)
    fact = "我喜欢喝绿茶"

    # SDK command subject is the only fact-write path.
    await nats_publish_assertion(
        handle.nats_url,
        user_id=handle.user_id,
        text=fact,
        wing="Wing_Profile",
        memory_type="preference",
    )

    async with mcp_session(handle.mcp_url) as session:

        async def _fact_landed(s) -> bool:
            return any(record.get("value") == fact for record in await _list_records(s))

        assert await wait_for_visible(session, predicate=_fact_landed, timeout_s=30)

        # The public privacy protocol owns archive/delete. Natural-language
        # interpretation belongs to the LLM steward and is not emulated with
        # phrase rules in a deterministic infrastructure E2E.
        archive_preview = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_forget_preview",
                {"target": "绿茶", "action": "archive"},
            )
        )
        assert archive_preview["status"] == "preview"
        archive_result = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_forget_confirm",
                {
                    "confirmation_token": archive_preview["confirmation_token"],
                    "wait_applied_seconds": 2.0,
                },
            )
        )
        assert archive_result["status"] in {"accepted", "applied"}

        async def _archived(s) -> bool:
            return any(
                record.get("value") == fact
                and (record.get("metadata") or {}).get("privacy") == "do_not_recall"
                for record in await _list_records(s)
            )

        assert await wait_for_visible(session, predicate=_archived, timeout_s=30)
        assert fact not in await _recall_values(session, context, "绿茶")

        # A later delete uses the same ledger-first coordinator and must also
        # remove an already archived projection.
        delete_preview = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_forget_preview",
                {"target": "绿茶", "action": "delete"},
            )
        )
        assert delete_preview["status"] == "preview"
        delete_result = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_forget_confirm",
                {
                    "confirmation_token": delete_preview["confirmation_token"],
                    "wait_applied_seconds": 2.0,
                },
            )
        )
        assert delete_result["status"] in {"accepted", "applied"}

        async def _deleted(s) -> bool:
            return all(record.get("value") != fact for record in await _list_records(s))

        assert await wait_for_visible(session, predicate=_deleted, timeout_s=30)


async def test_ambiguous_delete_requires_preview_then_exact_id_command(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_privacy_confirm",
        steward_mode="noop",
    )
    marker = "ambiguous-green-tea-marker"
    facts = [f"{marker} old preference", f"{marker} shopping history"]
    for fact in facts:
        await nats_publish_assertion(
            handle.nats_url,
            user_id=handle.user_id,
            text=fact,
            wing="Wing_Profile",
            memory_type="preference",
        )

    async with mcp_session(handle.mcp_url) as session:

        async def _both_landed(s) -> bool:
            values = {record.get("value") for record in await _list_records(s)}
            return all(fact in values for fact in facts)

        assert await wait_for_visible(session, predicate=_both_landed, timeout_s=30)

        preview = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_forget_preview",
                {"target": marker, "action": "delete"},
            )
        )
        assert preview["status"] == "preview"
        assert preview["requires_explicit_confirmation"] is True
        assert len(preview["candidates"]) == 2
        # Preview is provably read-only.
        assert await _both_landed(session)

        confirmed = mcp_tool_json(
            await session.call_tool(
                "eidolon_memory_forget_confirm",
                {
                    "confirmation_token": preview["confirmation_token"],
                    "wait_applied_seconds": 2.0,
                },
            )
        )
        assert confirmed["status"] in {"accepted", "applied"}

        async def _both_deleted(s) -> bool:
            values = {record.get("value") for record in await _list_records(s)}
            return all(fact not in values for fact in facts)

        assert await wait_for_visible(session, predicate=_both_deleted, timeout_s=30)
