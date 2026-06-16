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

from typing import TYPE_CHECKING

from eidolon.memory.domain.steward import StewardDecision

if TYPE_CHECKING:
    from eidolon_sdk.memory import ConversationTurnPayload


class NoOpSteward:
    """Steward implementation that intentionally does nothing.

    Returning ``should_write=False`` means the worker won't even iterate the
    (empty) fragments list — fastest possible path through ``turn_processor``.
    """

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        del turn  # ack-only path
        return StewardDecision(
            should_write=False,
            fragments=[],
            triples=[],
            invalidations=[],
            privacy_actions=[],
        )
