"""The write half of the memory service boundary.

Two kinds of write, distinguished by what the caller needs to know:

Conversation turns are published and forgotten. The caller is off the hook once
the bus accepts the message; absorption happens later, and the service decides
what — if anything — is worth remembering from the turn.

Explicit writes are different. When a user says "remember that I'm allergic to
peanuts", the assistant is about to tell them it has been remembered, so the
caller needs a truthful answer before it speaks. These calls wait for a durable
outcome and report one of the five write states, of which only ``applied`` means
stored and readable. A caller that says "I'll remember that" on ``accepted`` is
lying to the user.

Every write is idempotent on a caller-supplied id — ``turn_id`` for turns,
``request_id`` derived from the claim for explicit writes — so retrying is
always safe.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .payloads import ConversationTurnPayload, MemoryActorContext
from .results import ForgetOutcome, TurnPublishReceipt, WriteOutcome


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

    async def write_confirmed_fact(
        self,
        ctx: MemoryActorContext,
        text: str,
        *,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.99,
        tags: tuple[str, ...] = (),
        wait_applied_seconds: float = 0.75,
    ) -> WriteOutcome:
        """Store a fact the user explicitly asked to be remembered.

        Waits up to ``wait_applied_seconds`` for a durable outcome so the caller
        can be honest about what happened. On timeout the status is ``accepted``
        or ``unknown``, never ``applied``.

        ``text`` is stored as the user phrased it. ``source_event_id`` and
        ``tool_call_id`` anchor the write to the turn that requested it and make
        the derived ``request_id`` stable across retries.
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
