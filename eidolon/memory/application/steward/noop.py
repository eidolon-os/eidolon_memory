"""No-op steward: ack the turn without writing fragments or KG triples.

Use cases:
    * Bench / e2e tests where we want to exercise the worker plumbing
      (NATS subscribe → ack → metrics) without exercising the LLM extractor.
    * "Listen-only" runtime mode for debugging — keep the message bus
      flowing while triaging steward output offline.

Contract (matches the protocol ``turn_processor`` expects):

    async def decide(turn: ConversationTurnPayload) -> StewardDecision
        Returns an empty decision (should_write=False, no fragments,
        no triples, no privacy actions). The worker ACKs the turn cleanly.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from eidolon.memory.domain.steward import StewardDecision

if TYPE_CHECKING:
    from eidolon_memory_contracts import ConversationTurnPayload

# Test-only seam: when set, each ``decide`` sleeps this many seconds before
# returning. Lets e2e tests simulate a slow (LLM-like) turn steward without a
# real LLM — used to verify the agent_runner subscriber does NOT let a busy
# turn subject starve the command subject. Unset in all production paths.
_TEST_TURN_DELAY_ENV = "EIDOLON_MEMORY_TEST_TURN_DELAY_S"


class NoOpSteward:
    """Steward implementation that intentionally does nothing.

    Returning ``should_write=False`` means the worker won't even iterate the
    (empty) fragments list — fastest possible path through ``turn_processor``.
    """

    @property
    def extraction_version(self) -> str:
        return "noop:v1"

    async def aclose(self) -> None:
        pass

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        del turn  # ack-only path
        delay = os.environ.get(_TEST_TURN_DELAY_ENV, "").strip()
        if delay:
            try:
                await asyncio.sleep(float(delay))
            except ValueError:
                pass
        return StewardDecision(
            should_write=False,
            fragments=[],
            triples=[],
            invalidations=[],
            privacy_actions=[],
        )
