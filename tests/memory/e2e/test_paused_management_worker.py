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
        from eidolon_memory_contracts.subjects import provisional_sources_subject

        with pytest.raises(nats.errors.NoRespondersError):
            await nc.request(provisional_sources_subject(realm), b"{}", timeout=1)
    finally:
        await nc.close()


async def test_provisional_worker_consumes_only_sources_without_runtime_mcp(live_agent_runner):
    import asyncio
    import json
    import sqlite3
    from pathlib import Path

    from eidolon_memory_contracts import conversation_turn_subject, envelope_memory_payload

    from eidolon.memory.config.palace_directory import LEDGERS_DIR_SUFFIX

    realm = "e2e_provisional_person"
    handle = live_agent_runner(
        user_id=realm,
        management_only=True,
        provisional_ingest=True,
        provisional_recall=True,
        env_overrides={"EIDOLON_MEMORY_API_TOKEN": "provisional-worker-test-credential"},
    )
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{handle.port}", trust_env=False
    ) as client:
        assert (await client.post("/mcp")).status_code == 403
        assert (await client.post("/ops/mcp")).status_code == 403
    nc = await nats.connect(handle.nats_url)
    try:
        js = nc.jetstream()
        for attribution in ("confirmed", "provisional"):
            payload = dict(
                turn_id=attribution,
                context=dict(
                    memory_realm_id=realm,
                    owner_id="o",
                    person_id="p",
                    identity_epoch=1,
                    companion_id="c",
                    session_id="s",
                ),
                user_text="source retained without facts",
                assistant_text="",
                timestamp="2026-10-29T00:00:00Z",
                metadata={"person_attribution": attribution},
            )
            envelope = envelope_memory_payload(payload, kind="conversation_turn")
            await js.publish(conversation_turn_subject(realm), envelope.model_dump_json().encode())
        path = Path(str(handle.palace_dir) + LEDGERS_DIR_SUFFIX) / "extraction_decisions.sqlite3"
        records = []
        for _ in range(50):
            with sqlite3.connect(path) as conn:
                records = conn.execute(
                    "SELECT source_turn_id, extractor_version, decision_json, intents_json "
                    "FROM extraction_decisions"
                ).fetchall()
            if records:
                break
            await asyncio.sleep(0.1)
        assert len(records) == 1
        source_id, version, raw, intents = records[0]
        assert source_id == "provisional" and version == "provisional-source-v1"
        decision = json.loads(raw)
        assert decision["source_turn"]["context"]["person_id"] == "p"
        assert decision["fragments"] == decision["triples"] == [] and json.loads(intents) == []
        from eidolon.memory.config.memory_settings import load_memory_settings

        prefix = load_memory_settings(handle.settings_path).nats.durable_prefix
        names = {
            c.name
            for stream in await js.streams_info()
            for c in await js.consumers_info(stream.config.name)
        }
        assert memory_consumer_name(prefix, realm, role="turn") in names
        assert memory_consumer_name(prefix, realm, role="sync") not in names
        from eidolon_memory_contracts.subjects import provisional_sources_subject

        ctx = dict(
            memory_realm_id=realm,
            owner_id="o",
            person_id="p",
            identity_epoch=2,
            companion_id="c",
            session_id="s",
        )

        async def query(context):
            reply = await nc.request(
                provisional_sources_subject(realm),
                json.dumps({"context": context}).encode(),
                timeout=1,
            )
            return json.loads(reply.data)

        matching = await query(ctx)
        assert matching["identity_binding"]["identity_epoch"] == 2
        assert matching["sources"][0]["turn_id"] == "provisional"
        assert (await query({**ctx, "session_id": "other"}))["sources"] == []
        assert (await query({**ctx, "person_id": "other"}))["sources"] == []
        assert "error" in await query({**ctx, "memory_realm_id": "other"})
    finally:
        await nc.close()
