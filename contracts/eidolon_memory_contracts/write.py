"""The write half of the memory service boundary.

Conversation turns are published and forgotten. The caller is off the hook once
the bus accepts the message; absorption happens later, and the service decides
what — if anything — is worth remembering from the turn.
Privacy confirmation waits for a ledger-backed outcome because it must never
claim a deletion that has not happened. Both writes are idempotent on their
contract identities.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .payloads import ConversationTurnPayload, MemoryActorContext
from .results import ForgetOutcome, TurnPublishReceipt


@runtime_checkable
class MemoryWriteContract(Protocol):
    """Everything a client may write to memory."""

    async def publish_turn(
        self,
        turn: ConversationTurnPayload,
        *,
        trace_id: str | None = None,
    ) -> TurnPublishReceipt:
        """Hand a completed turn to memory.

        Fire-and-forget by design: this returns as soon as the bus has the
        message. The receipt says whether publishing succeeded, not whether
        memory kept anything. Deduplicated by ``turn.turn_id``.
        """
        ...

    async def confirm_forget(
        self,
        ctx: MemoryActorContext,
        confirmation_token: str,
        *,
        wait_applied_seconds: float = 2.0,
    ) -> ForgetOutcome:
        """Commit a privacy request previewed earlier.

        The token comes from ``MemoryReadContract.preview_forget`` and is scoped
        to the candidates the user saw. An expired or unknown token fails; it
        never falls back to a broader deletion.
        """
        ...
