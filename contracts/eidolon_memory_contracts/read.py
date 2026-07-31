"""The read half of the memory service boundary.

Reads are synchronous and sit on the critical path of a response, so the
contract's central promise is that they never raise. A read that times out,
finds the service down, or fails for any other reason returns its result with
``degraded=True`` and a reason, leaving the caller free to answer with less
context rather than not at all.

Consequences for implementors:

- Never propagate an exception, including cancellation-adjacent errors from a
  transport. Convert to a degraded result.
- Honour ``timeout_s`` as a deadline for the whole call, not per attempt.
- A degraded result must still be structurally valid — empty snippets and an
  empty context string, not None.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .payloads import MemoryActorContext
from .results import (
    CommitmentReadResult,
    ForgetAction,
    ForgetPreview,
    RecallPlan,
    RecallResult,
    SearchResult,
    ServiceStatus,
    SourceTurnLookup,
    WriteOutcome,
)


@runtime_checkable
class MemoryReadContract(Protocol):
    """Everything a client may read from memory."""

    async def recall_context(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        plan: RecallPlan,
        timeout_s: float = 0.2,
    ) -> RecallResult:
        """Recall what is relevant to ``query`` for this caller.

        This is the conversational read: the service decides what matters, how
        to rank it and how to present it. Callers pass intent (the query, the
        plan) and get prompt-ready context back.
        """
        ...

    async def search(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        top_k: int = 5,
        timeout_s: float = 0.2,
    ) -> SearchResult:
        """Look up memories matching ``query``.

        For when a user explicitly asks what is remembered, rather than for
        assembling conversational context.
        """
        ...

    async def read_active_commitments(
        self,
        ctx: MemoryActorContext,
        *,
        limit: int = 5,
        timeout_s: float = 0.2,
    ) -> CommitmentReadResult:
        """Read promises still in play for this caller."""
        ...

    async def get_by_source_turn(
        self,
        ctx: MemoryActorContext,
        source_turn_id: str,
        *,
        timeout_s: float = 0.5,
    ) -> SourceTurnLookup:
        """Report what a published turn produced, if it has been absorbed."""
        ...

    async def preview_forget(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        action: ForgetAction = "archive",
        timeout_s: float = 0.5,
    ) -> ForgetPreview:
        """Resolve a natural-language privacy request without changing anything.

        Lives on the read side because it has no side effects. Committing is a
        write — see ``MemoryWriteContract.confirm_forget``.
        """
        ...

    async def command_status(
        self,
        ctx: MemoryActorContext,
        request_id: str,
        *,
        timeout_s: float = 0.5,
    ) -> WriteOutcome:
        """Resolve the current outcome of an earlier non-terminal write."""
        ...

    async def status(self, ctx: MemoryActorContext) -> ServiceStatus:
        """Operational summary, for diagnostics rather than control flow."""
        ...

    async def health(self) -> bool:
        """True when the service is reachable and serving this caller's space."""
        ...
