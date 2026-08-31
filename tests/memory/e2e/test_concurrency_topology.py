"""E2E contracts for the per-Realm single-owner concurrency topology."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_assertion,
    tail_file,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _listed_records(session) -> list[dict]:
    result = await session.call_tool(
        "eidolon_memory_list",
        {"limit": 1000, "include_private": True},
    )
    payload = mcp_tool_json(result)
    if not isinstance(payload, dict):
        return []
    rows = payload.get("records") or []
    return [row for row in rows if isinstance(row, dict)]


async def _marker_count(session, marker: str) -> int:
    return sum(marker in str(row) for row in await _listed_records(session))


async def test_second_agent_for_same_realm_is_rejected_before_serving(
    live_agent_runner,
) -> None:
    owner = live_agent_runner(
        user_id="e2e_single_owner",
        steward_mode="noop",
    )
    duplicate_log = owner.log_path.with_name("duplicate-agent.log")
    agent_cli = Path(sys.executable).parent / "eidolon-memory-agent"
    if not agent_cli.is_file():
        resolved = shutil.which("eidolon-memory-agent")
        assert resolved is not None
        agent_cli = Path(resolved)
    env = {**os.environ, "EIDOLON_MEMORY_SETTINGS_YAML": str(owner.settings_path)}

    with duplicate_log.open("ab") as log_fp:
        duplicate = subprocess.Popen(
            [
                str(agent_cli),
                "--memory-space-id",
                owner.user_id,
                "--port",
                "19111",
            ],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 15
        while duplicate.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert duplicate.poll() is not None, "duplicate Realm owner kept running"
        assert duplicate.returncode != 0
        assert "already owned by another eidolon-memory-agent" in tail_file(
            duplicate_log,
            max_chars=8000,
        )
        assert owner.process.poll() is None, "original Realm owner was disturbed"
    finally:
        if duplicate.poll() is None:
            duplicate.terminate()
            duplicate.wait(timeout=5)


async def test_parallel_realms_keep_ports_palaces_and_records_isolated(
    live_agent_runner,
    mcp_session,
) -> None:
    realm_a = live_agent_runner(
        user_id="e2e_parallel_realm_a",
        steward_mode="noop",
    )
    realm_b = live_agent_runner(
        user_id="e2e_parallel_realm_b",
        steward_mode="noop",
    )
    marker_a = f"realm-a-{uuid.uuid4().hex}"
    marker_b = f"realm-b-{uuid.uuid4().hex}"

    await asyncio.gather(
        nats_publish_assertion(
            realm_a.nats_url,
            user_id=realm_a.user_id,
            text=marker_a,
            request_id=f"request-{marker_a}",
        ),
        nats_publish_assertion(
            realm_b.nats_url,
            user_id=realm_b.user_id,
            text=marker_b,
            request_id=f"request-{marker_b}",
        ),
    )

    async with mcp_session(realm_a.mcp_url) as session_a, mcp_session(realm_b.mcp_url) as session_b:
        assert await wait_for_visible(
            session_a,
            predicate=lambda s: _marker_count(s, marker_a),
            timeout_s=60,
        )
        assert await wait_for_visible(
            session_b,
            predicate=lambda s: _marker_count(s, marker_b),
            timeout_s=60,
        )
        assert await _marker_count(session_a, marker_b) == 0
        assert await _marker_count(session_b, marker_a) == 0

    assert realm_a.port != realm_b.port
    assert realm_a.palace_dir != realm_b.palace_dir
    assert realm_a.palace_dir.is_dir()
    assert realm_b.palace_dir.is_dir()


async def test_concurrent_redelivery_is_idempotent_and_unique_writes_remain_visible(
    live_agent_runner,
    mcp_session,
) -> None:
    handle = live_agent_runner(
        user_id="e2e_concurrent_replay",
        steward_mode="noop",
    )
    replay_marker = f"replay-{uuid.uuid4().hex}"
    replay_request = f"request-{uuid.uuid4().hex}"

    await asyncio.gather(
        *(
            nats_publish_assertion(
                handle.nats_url,
                user_id=handle.user_id,
                text=replay_marker,
                request_id=replay_request,
            )
            for _ in range(12)
        )
    )

    unique_markers = [f"unique-{uuid.uuid4().hex}" for _ in range(8)]
    await asyncio.gather(
        *(
            nats_publish_assertion(
                handle.nats_url,
                user_id=handle.user_id,
                text=marker,
                request_id=f"request-{marker}",
            )
            for marker in unique_markers
        )
    )

    async with mcp_session(handle.mcp_url) as session:
        assert await wait_for_visible(
            session,
            predicate=lambda s: _marker_count(s, replay_marker),
            timeout_s=60,
        )
        for marker in unique_markers:
            assert await wait_for_visible(
                session,
                predicate=lambda s, marker=marker: _marker_count(s, marker),
                timeout_s=60,
            )
        assert await _marker_count(session, replay_marker) == 1
