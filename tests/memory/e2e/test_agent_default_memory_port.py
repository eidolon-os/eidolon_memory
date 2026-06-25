"""Agent-side e2e for the default owner's memory read/write path.

This test intentionally uses the agent's public memory port adapter while the
memory service runs as a real subprocess:

    agent EidolonMemoryPort.write_turn -> NATS -> eidolon-memory-agent
      -> MCP recall via agent McpClientPool -> rendered working-memory context

It avoids LLM / long-term extraction assumptions by asserting against the
process-local working-memory section.
"""

import asyncio
import inspect
import time
import uuid

import pytest
from eidolon_agent.config.settings import MemoryEndpoint, NatsSettings
from eidolon_agent.core.types.memory import MemoryQueryPlan
from eidolon_agent.infra.events.nats_bus import NatsEventBus
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

MEMORY_SPACE_ID = "default.default.agent_e2e"
OWNER_USER_ID = "default"
TENANT_ID = "default"
COMPANION_ID = "agent_e2e"
SESSION_ID = "agent-e2e-default"


async def _wait_for_true(
    predicate,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(poll_interval_s)
    return False


async def test_agent_memory_port_writes_and_recalls_default_user(live_agent_runner) -> None:
    handle = live_agent_runner(
        user_id=MEMORY_SPACE_ID,
        port=19130,
        steward_mode="noop",
    )
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id=MEMORY_SPACE_ID,
                mcp_url=handle.mcp_url,
            )
        ],
        nats=NatsSettings(url=handle.nats_url),
    )
    bus = NatsEventBus(handle.nats_url)
    pool = McpClientPool(routes=routes)
    port = EidolonMemoryPort(
        pool=pool,
        publisher=MemoryNatsPublisher(event_bus=bus, routes=routes),
    )
    marker = f"agent-default-memory-e2e-{uuid.uuid4().hex[:8]}"
    try:
        await port.write_turn(
            OWNER_USER_ID,
            SESSION_ID,
            f"turn-{marker}",
            f"这只是端到端测试标记: {marker}, 不要长期记住。",
            "好的, 我只在这次会话里保留它。",
            tenant_id=TENANT_ID,
            companion_id=COMPANION_ID,
            agent_id="agent-e2e",
            device_id="default-device",
            metadata={"source": "agent-default-memory-e2e"},
        )

        async def _recall_contains_marker() -> bool:
            result = await port.recall_context(
                OWNER_USER_ID,
                "刚才的端到端测试标记是什么?",
                plan=MemoryQueryPlan(semantic_k=5, voice=False),
                timeout_s=5.0,
                tenant_id=TENANT_ID,
                companion_id=COMPANION_ID,
                agent_id="agent-e2e",
                device_id="default-device",
                session_id=SESSION_ID,
            )
            assert result.degraded is False
            return marker in result.context

        assert await _wait_for_true(
            _recall_contains_marker,
            timeout_s=30,
            poll_interval_s=0.5,
        ), f"agent memory port did not recall the turn written for {MEMORY_SPACE_ID}"
    finally:
        await port.close()
        await bus.close()
