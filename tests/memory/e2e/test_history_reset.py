"""Run the production reset CLI against an isolated broker and stopped test realm."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import nats
import pytest
import yaml
from eidolon_memory_contracts import (
    conversation_turn_subject,
    memory_command_subject,
    memory_sync_subject,
)
from nats.js.errors import NotFoundError

from eidolon.memory.infrastructure.history_reset import CONFIRMATION
from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_assertion,
    nats_publish_turn,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_reset_clears_storage_and_all_replay_subjects_but_preserves_other_realm(
    live_agent_runner,
    mcp_session,
    tmp_path,
):
    assert not os.environ.get("EIDOLON_MEMORY_E2E_NATS_URL"), (
        "destructive reset E2E requires its own fixture-managed broker"
    )
    root = tmp_path / "reset-palaces"
    logs = tmp_path / "reset-logs"
    target = live_agent_runner(
        user_id="reset-target",
        palace_root_override=root,
        extra_settings={"runtime": {"log_dir": str(logs)}},
    )
    other = live_agent_runner(user_id="reset-neighbor")
    marker = "person:reset likes topic:old"
    old_request = await nats_publish_assertion(
        target.nats_url,
        user_id=target.user_id,
        text=marker,
        wing="Wing_Life",
        subject="person:reset",
        predicate="likes",
        object_value="topic:old",
    )
    await nats_publish_assertion(other.nats_url, user_id=other.user_id, text="neighbor survives")

    async def records(session):
        result = mcp_tool_json(await session.call_tool("eidolon_memory_list", {"limit": 100}))
        return result["records"]

    async with mcp_session(target.mcp_url) as session:

        async def landed(s):
            return any(row["value"] == marker for row in await records(s))

        assert await wait_for_visible(session, predicate=landed, timeout_s=30)
        graph = mcp_tool_json(
            await session.call_tool("eidolon_memory_kg_snapshot", {"max_triples": 100})
        )
        assert any(t["object"] == "topic:old" for t in graph["triples"])
    target.kill()
    assert target.process.returncode == 0

    # These messages exist before the reset, while there is no consumer process.
    await nats_publish_turn(
        target.nats_url,
        user_id=target.user_id,
        user_text="old queued turn",
        turn_id="old-queued-turn",
    )
    await nats_publish_assertion(target.nats_url, user_id=target.user_id, text="old queued command")
    client = await nats.connect(target.nats_url)
    try:
        js = client.jetstream()
        await js.publish(memory_sync_subject(target.user_id), b'{"old_sync_marker": true}')
        for subject in (
            conversation_turn_subject(target.user_id),
            memory_command_subject(target.user_id),
            memory_sync_subject(target.user_id),
        ):
            assert (await js.get_last_msg("MEMORY_TURNS", subject)).data
    finally:
        await client.close()

    registry = tmp_path / "registry.sqlite3"
    with sqlite3.connect(registry) as connection:
        connection.execute("CREATE TABLE memory_realms (realm_id, owner_id, companion_id, status)")
        connection.execute(
            "INSERT INTO memory_realms VALUES (?, 'e2e', 'e2e', 'active')", (target.user_id,)
        )
    config = yaml.safe_load(target.settings_path.read_text())
    report_path = tmp_path / "reset-report.json"
    env = {
        **os.environ,
        "EIDOLON_MEMORY_RUN_DIR": config["runtime"]["run_dir"],
        "EIDOLON_MEMORY_LOG_DIR": str(logs),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "scripts/reset_memory_history_keep_realms.py",
        "--settings",
        str(target.settings_path),
        "--registry-db",
        str(registry),
        "--palaces-root",
        str(root),
        "--report",
        str(report_path),
        "--confirm",
        CONFIRMATION,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    report = json.loads(report_path.read_text())
    assert report["registry_sha256_before"] == report["registry_sha256_after"]
    assert len(report["nats"]["purged_subjects"]) == 3
    assert list(target.palace_dir.iterdir()) == []
    assert list(Path(str(target.palace_dir) + ".ledgers").iterdir()) == []

    client = await nats.connect(target.nats_url)
    try:
        for subject in report["nats"]["purged_subjects"]:
            with pytest.raises(NotFoundError):
                await client.jetstream().get_last_msg("MEMORY_TURNS", subject)
    finally:
        await client.close()

    restarted = live_agent_runner(
        user_id=target.user_id, palace_root_override=root, keep_palace=True
    )
    async with mcp_session(restarted.mcp_url) as session:
        assert await records(session) == []
        graph = mcp_tool_json(
            await session.call_tool("eidolon_memory_kg_snapshot", {"max_triples": 100})
        )
        assert graph["triples"] == []
        status = mcp_tool_json(
            await session.call_tool("eidolon_memory_command_status", {"request_id": old_request})
        )
        assert status.get("status") != "applied"
        await nats_publish_assertion(
            restarted.nats_url, user_id=restarted.user_id, text="new history"
        )

        async def new_visible(s):
            values = [row["value"] for row in await records(s)]
            assert marker not in values and "old queued command" not in values
            return "new history" in values

        assert await wait_for_visible(session, predicate=new_visible, timeout_s=30)
    async with mcp_session(other.mcp_url) as session:
        assert [r["value"] for r in await records(session)] == ["neighbor survives"]
