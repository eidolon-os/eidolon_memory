"""Real paused worker keeps authenticated management but has no runtime consumers."""

import httpx
import nats
import pytest

from eidolon.memory.infrastructure.nats.names import memory_consumer_name

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_paused_worker_management_and_subscriptions(live_agent_runner):
    realm = "e2e_paused_person"
    token = "paused-management-test-credential"
    handle = live_agent_runner(
        user_id=realm, management_only=True, env_overrides={"EIDOLON_MEMORY_API_TOKEN": token}
    )
    base = f"http://127.0.0.1:{handle.port}"
    async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
        assert (await client.get("/api/memory/v1/status")).status_code in (401, 403)
        assert (
            await client.get("/api/memory/v1/status", headers={"Authorization": "Bearer " + token})
        ).status_code == 200
        assert (await client.post("/mcp")).status_code == 403
        assert (await client.post("/ops/mcp")).status_code == 403
    nc = await nats.connect(handle.nats_url)
    try:
        js = nc.jetstream()
        streams = await js.streams_info()
        names = {c.name for stream in streams for c in await js.consumers_info(stream.config.name)}
        # The default durable prefix is read from the same settings as the runner.
        from eidolon.memory.config.memory_settings import load_memory_settings

        prefix = load_memory_settings(handle.settings_path).nats.durable_prefix
        assert memory_consumer_name(prefix, realm, role="cmd") in names
        assert memory_consumer_name(prefix, realm, role="turn") not in names
        assert memory_consumer_name(prefix, realm, role="sync") not in names
    finally:
        await nc.close()
