"""Regression e2e — a busy turn subject must NOT starve the command subject.

The agent_runner drains turns and commands on one NATS connection. Turns are
processed serially and each spends seconds in the steward (LLM in prod), so a
burst of turns used to block command processing (admin KG edits, user-confirmed
facts, consolidator theme writes) for minutes — commands sat unapplied behind
the turn backlog. The fix runs each subject's drain in its own task; since the
steward's slow work happens outside the per-operation backend lock, a command
acquires the lock and applies within ~1-2s even while a long turn backlog churns.

This test drives that edge deterministically WITHOUT an LLM: a noop steward with
``EIDOLON_MEMORY_TEST_TURN_DELAY_S`` set makes each turn take a fixed time, so a
15-turn burst is a ~30s backlog. A KG-add command published mid-backlog must
land well before the backlog could possibly drain.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.memory.e2e.conftest import (
    mcp_tool_json,
    nats_publish_kg_add_triple,
    nats_publish_turn,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

_TURN_DELAY_S = 2.0
_N_TURNS = 15  # ~30s serial backlog — far longer than the assertion window
_CMD_DEADLINE_S = 10.0  # fix applies in ~1-2s; pre-fix would need ~_N_TURNS*delay


async def _triples_total(session) -> int:
    payload = mcp_tool_json(await session.call_tool("eidolon_memory_kg_stats", {}))
    return int(payload.get("triples_total") or 0) if isinstance(payload, dict) else 0


async def test_command_applied_promptly_while_turn_backlog_churns(
    live_agent_runner, mcp_session
):
    handle = live_agent_runner(
        user_id="e2e_cmd_starve", port=19095, steward_mode="noop",
        env_overrides={"EIDOLON_MEMORY_TEST_TURN_DELAY_S": str(_TURN_DELAY_S)},
    )

    # Fill the turn subject with a burst that will take ~_N_TURNS * delay to
    # drain serially. noop steward writes no fragments, but each turn still
    # sleeps _TURN_DELAY_S in decide().
    for i in range(_N_TURNS):
        await nats_publish_turn(
            handle.nats_url,
            user_id=handle.user_id,
            user_text=f"backlog turn {i}",
            assistant_text="ok",
            turn_id=f"starve-{i:02d}",
        )

    # Give the turn task time to fetch the burst and get busy processing it.
    await asyncio.sleep(_TURN_DELAY_S + 1.0)

    async with mcp_session(handle.mcp_url) as session:
        # Sanity: no triples yet (noop steward emits none).
        assert await _triples_total(session) == 0, "unexpected pre-existing triples"

        # Publish a KG-add command while the turn backlog is still draining.
        await nats_publish_kg_add_triple(
            handle.nats_url, user_id=handle.user_id,
            subject="self", predicate="likes", obj="oolong", confidence=0.95,
        )

        # The command must land promptly — NOT wait for the ~30s turn backlog.
        deadline = time.monotonic() + _CMD_DEADLINE_S
        landed_at: float | None = None
        while time.monotonic() < deadline:
            if await _triples_total(session) >= 1:
                landed_at = time.monotonic()
                break
            await asyncio.sleep(0.25)

        assert landed_at is not None, (
            f"KG-add command did not apply within {_CMD_DEADLINE_S}s while a "
            f"~{_N_TURNS * _TURN_DELAY_S:.0f}s turn backlog was draining — the "
            f"command subject is being starved by the turn subject."
        )
