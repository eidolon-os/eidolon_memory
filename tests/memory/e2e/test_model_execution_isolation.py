"""Real MCP and NATS stay live during synchronous model initialization/inference."""

import asyncio

import pytest

from eidolon.memory.application.discovery import probe_mcp_http
from tests.memory.e2e.conftest import mcp_tool_json, nats_publish_turn, wait_for_visible
from tests.memory.llm_fixture import model_sdk_fixture  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_recall_and_discovery_survive_blocking_model_then_see_written_fact(
    live_agent_runner, mcp_session, isolated_model_sdk, monkeypatch
):
    monkeypatch.setenv("TEST_LLM_IMPORT_DELAY", "3")
    monkeypatch.setenv("TEST_LLM_CALL_DELAY", "3")
    handle = live_agent_runner(
        user_id="model_isolation",
        steward_mode="llm",
        extra_settings={"llm": {"model": "openai/test", "timeout_seconds": 15}},
    )
    async with mcp_session(handle.mcp_url) as session:
        await nats_publish_turn(
            handle.nats_url,
            user_id=handle.user_id,
            user_text="我喜欢乌龙茶",
            assistant_text="好的",
            turn_id="isolated-model-turn",
        )
        for stage in ("import_started", "call_started"):
            async with asyncio.timeout(15):
                while (
                    not isolated_model_sdk.exists() or stage not in isolated_model_sdk.read_text()
                ):
                    await asyncio.sleep(0.02)
            assert "call_finished" not in isolated_model_sdk.read_text()
            # Exercise actual MCP request handling, not just a loop heartbeat.
            payload = mcp_tool_json(
                await asyncio.wait_for(
                    session.call_tool("eidolon_memory_list", {"limit": 10}),
                    timeout=0.5,
                )
            )
            assert payload.get("records", []) == []
            assert await probe_mcp_http(handle.agent_mcp_url, timeout_seconds=0.5)

        async def fact_visible(s):
            result = mcp_tool_json(await s.call_tool("eidolon_memory_list", {"limit": 10}))
            return any("乌龙茶" in row.get("value", "") for row in result.get("records", []))

        assert await wait_for_visible(session, predicate=fact_visible, timeout_s=20)

    async with mcp_session(handle.agent_mcp_url) as reader:
        recalled = mcp_tool_json(
            await reader.call_tool(
                "eidolon_memory_recall_context",
                {
                    "query": "用户喜欢乌龙茶",
                    "voice": True,
                    "context": {
                        "memory_realm_id": handle.user_id,
                        "owner_id": "e2e",
                        "companion_id": "e2e",
                        "device_id": "e2e",
                        "session_id": "e2e",
                    },
                },
            )
        )
        assert not recalled.get("degraded"), recalled
        assert "乌龙茶" in recalled.get("context", ""), recalled
