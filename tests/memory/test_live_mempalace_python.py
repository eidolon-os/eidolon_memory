"""Integration tests against the real MemPalace Python package (D1).

Skipped unless ``mempalace`` is installed and ``EIDOLON_MEMORY_RUN_LIVE`` is set.
``McpRecallClient`` tests removed in D1 — backend.search is exercised directly
by the agent_runner code path which is covered by other integration suites.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from eidolon_memory_contracts import ConversationTurnPayload

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.application.steward.rules import RuleBasedSteward


def _require_live() -> None:
    if os.environ.get("EIDOLON_MEMORY_RUN_LIVE", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("set EIDOLON_MEMORY_RUN_LIVE=1 to run real MemPalace integration tests")
    pytest.importorskip("mempalace")


@pytest.mark.mempalace
@pytest.mark.asyncio
async def test_live_python_backend_ingest_search(live_memory_settings, test_palace_dir):
    _require_live()
    backend = MemPalacePythonBackend(live_memory_settings, str(test_palace_dir))
    token = f"eidolon_python_live_{uuid.uuid4().hex}"
    wing = live_memory_settings.wings[0].id
    room = f"room_{uuid.uuid4().hex[:8]}"
    await backend.ingest_text(
        wing=wing,
        room=room,
        text=f"pytest live ingest marker {token}",
        metadata=None,
    )
    found = False
    for _ in range(80):
        hits = await backend.search(token, wing=wing, n_results=12)
        if any(token in str(h.value) for h in hits):
            found = True
            break
        await asyncio.sleep(0.5)
    assert found


@pytest.mark.mempalace
@pytest.mark.asyncio
async def test_live_rules_steward_turn(live_memory_settings, test_palace_dir):
    _require_live()
    backend = MemPalacePythonBackend(live_memory_settings, str(test_palace_dir))
    token = f"eidolon_turn_{uuid.uuid4().hex}"
    wing = "user_local_001"
    room = f"session_{uuid.uuid4().hex[:8]}"
    turn = ConversationTurnPayload(
        turn_id=uuid.uuid4().hex,
        user_text=f"我最近因为项目 deadline 很焦虑，测试标记 {token}",
        assistant_text="assistant ack",
        timestamp="2026-05-11T12:00:00Z",
        session_id=room,
        user_id=wing,
        metadata=None,
    )
    await RuleBasedSteward(live_memory_settings).handle_turn(turn, backend)
    found = False
    for _ in range(80):
        hits = await backend.search(token, wing="Wing_Emotion", n_results=12)
        if any(token in str(h.value) for h in hits):
            found = True
            break
        await asyncio.sleep(0.5)
    assert found
