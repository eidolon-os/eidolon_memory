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


async def test_replayed_turn_after_restart_does_not_reextract_or_duplicate(
    live_agent_runner,
    mcp_session,
    isolated_model_sdk,
):
    import nats

    from eidolon.memory.config.memory_settings import MemorySettings
    from eidolon.memory.infrastructure.nats.names import memory_consumer_name

    settings = {"llm": {"model": "openai/test", "timeout_seconds": 15}}
    handle = live_agent_runner(user_id="model-replay", steward_mode="llm", extra_settings=settings)
    turn_id = "persisted-decision-turn"
    await nats_publish_turn(
        handle.nats_url,
        user_id=handle.user_id,
        user_text="我喜欢乌龙茶",
        assistant_text="好的",
        turn_id=turn_id,
    )

    async def rows(session):
        return mcp_tool_json(await session.call_tool("eidolon_memory_list", {"limit": 100}))[
            "records"
        ]

    async with mcp_session(handle.mcp_url) as session:

        async def landed(s):
            return any("乌龙茶" in row["value"] for row in await rows(s))

        assert await wait_for_visible(session, predicate=landed, timeout_s=20)
        before = await rows(session)
    assert isolated_model_sdk.read_text().count("call_started") == 1
    handle.kill()
    assert handle.process.returncode == 0
    restarted = live_agent_runner(
        user_id=handle.user_id, steward_mode="llm", keep_palace=True, extra_settings=settings
    )
    client = await nats.connect(handle.nats_url)
    try:
        js = client.jetstream()
        cfg = MemorySettings().nats
        durable = memory_consumer_name(cfg.durable_prefix, handle.user_id)
        previous = (await js.consumer_info(cfg.stream, durable)).delivered.consumer_seq
        await nats_publish_turn(
            handle.nats_url,
            user_id=handle.user_id,
            user_text="我喜欢乌龙茶",
            assistant_text="好的",
            turn_id=turn_id,
        )
        # Observe a new delivery and ACK, so a quick unchanged read cannot pass prematurely.
        async with asyncio.timeout(20):
            while True:
                state = await js.consumer_info(cfg.stream, durable)
                if (
                    state.delivered.consumer_seq > previous
                    and state.num_pending == 0
                    and state.num_ack_pending == 0
                ):
                    break
                await asyncio.sleep(0.05)
    finally:
        await client.close()
    async with mcp_session(restarted.mcp_url) as session:
        after = await rows(session)
        assert {row["key"] for row in after} == {row["key"] for row in before}
    assert isolated_model_sdk.read_text().count("call_started") == 1
