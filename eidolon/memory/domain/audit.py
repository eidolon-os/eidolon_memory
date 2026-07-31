"""Where the service reports what it did with a published turn.

The publisher records that it handed a turn over; this is the other half — the
service saying whether it absorbed the turn or dropped it, so an operator can
see end-to-end delivery instead of only the attempt.

Auditing is observation, not part of the write. An audit sink that fails, or is
absent entirely, must never affect whether a turn is processed. The turn path
treats a missing sink as "no audit configured" and carries on.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AuditSinkPort(Protocol):
    """Records the outcome of absorbing one turn.

    Implementations must swallow their own failures. Raising here would fail a
    turn for a bookkeeping problem.
    """

    async def record_absorbed(
        self,
        turn: Any,
        *,
        trace_id: str,
        should_write: bool,
        fragments: int,
        triples: int,
    ) -> None:
        """Note that a turn was processed, and what it produced."""
        ...

    async def record_rejected(
        self,
        turn: Any,
        *,
        trace_id: str,
        reason: str,
        deliveries: int,
    ) -> None:
        """Note that a turn was not absorbed, and why."""
        ...
