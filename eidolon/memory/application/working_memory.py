"""Phase 2 — in-memory short-term conversation ring.

Why
---
Cosine + KG recall is great for "what did the user tell me last week"
but useless for "what did the user just say". Companion conversation has
a continuous-context requirement that chromadb's nearest-neighbor search
fundamentally can't serve — the immediate window is what holds the thread
of conversation together.

Design
------
- A bounded ``deque`` of verbatim ``ConversationTurnPayload`` objects.
- Lives **on** ``LockedBackend`` (same ``SpaceLock``) so we get serialised
  access for free — no separate lock to reason about, no deadlock risk
  across the backend write path.
- In-process only. ``agent_runner`` restart wipes it; long-term memory
  lives in chromadb + KG, which **do** persist. Trying to persist this
  ring would conflate "the conversation thread I'm currently in" with
  "what I know about you" — keeping them separate is the whole point.
- ``maxlen=0`` is a first-class disable, so production can roll back to
  pre-Phase-2 behaviour by config alone.

Memory budget
-------------
~1-2 KB per turn (Chinese conversation, including assistant_text). Default
``maxlen=10`` → ~20 KB per user. Even 10 concurrent companion users only
costs 200 KB total. Forget any growth concern.
"""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from typing import TYPE_CHECKING

from eidolon.memory.domain.space_lock import SpaceLock

if TYPE_CHECKING:
    from eidolon_memory_contracts import ConversationTurnPayload


class WorkingMemoryRing:
    """Bounded ring of recent verbatim turns.

    Thread-safety / D1 contract: shares ``LockedBackend.lock`` so the
    ring's mutations are interleaved with backend writes — the same
    invariant the rest of the read/write path relies on.

    That lock is a readers-writer lock. ``snapshot`` takes the reader side, so
    reading the ring during a recall does not queue behind another recall; the two
    mutators take the writer side. Sharing the vector store's lock still buys what
    it always did — one lock per space to reason about — and now it also means a
    recall reading the ring runs alongside the vector search rather than after it.
    """

    __slots__ = ("_bufs", "_lock", "_maxlen")

    def __init__(self, *, maxlen: int, lock: SpaceLock) -> None:
        if maxlen < 0:
            msg = f"working_memory_maxlen must be >= 0, got {maxlen}"
            raise ValueError(msg)
        # ``deque(maxlen=0)`` exists but silently drops everything — we keep
        # it as the canonical "disabled" sentinel so callers can pass the
        # config value verbatim without branching.
        self._bufs: dict[tuple[str, str], deque[ConversationTurnPayload]] = defaultdict(
            lambda: deque(maxlen=max(0, maxlen))
        )
        self._lock = lock
        self._maxlen = maxlen

    @property
    def maxlen(self) -> int:
        """The configured maximum. ``0`` means the ring is disabled."""
        return self._maxlen

    @property
    def enabled(self) -> bool:
        return self._maxlen > 0

    async def append(self, turn: ConversationTurnPayload) -> None:
        """Add the turn to the ring; oldest is evicted on overflow.

        No-op when the ring is disabled (``maxlen=0``).
        """
        if not self.enabled:
            return
        if not turn.context.device_id or not turn.context.session_id:
            return
        key = (turn.context.device_id, turn.context.session_id)
        async with self._lock.writer():
            self._bufs[key].append(turn)

    async def snapshot(
        self,
        *,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> list[ConversationTurnPayload]:
        """Return a shallow-deepcopy of the current ring contents.

        The deepcopy is intentional: callers (renderer / MCP envelope)
        shouldn't be able to mutate the ring through the returned list,
        and the schema is small enough that the copy cost is negligible
        (≤ 10 turns × ~2KB ≈ < 50μs).
        """
        if not self.enabled:
            return []
        async with self._lock.reader():
            if device_id is not None and session_id is not None:
                return [deepcopy(t) for t in self._bufs.get((device_id, session_id), [])]
            turns: list[ConversationTurnPayload] = []
            for buf in self._bufs.values():
                turns.extend(buf)
            turns.sort(key=lambda t: t.timestamp)
            return [deepcopy(t) for t in turns[-self._maxlen :]]

    async def clear(self) -> None:
        """Drop all turns. Used on session boundaries (TBD) and in tests."""
        async with self._lock.writer():
            self._bufs.clear()
