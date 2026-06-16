"""Phase 2 — unit tests for ``WorkingMemoryRing``.

Pure module tests:no backend, no NATS, no MCP. Just the ring's contract:
append / snapshot / overflow / disable / concurrent safety.
"""

from __future__ import annotations

import asyncio

import pytest

from eidolon.memory.application.working_memory import WorkingMemoryRing
from eidolon_sdk.memory import ConversationTurnPayload

pytestmark = pytest.mark.asyncio


def _turn(i: int) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id=f"t-{i}",
        user_text=f"user {i}",
        assistant_text=f"asst {i}",
        timestamp="2026-05-25T00:00:00Z",
        session_id="unit",
        user_id="alice",
    )


# ─── Construction ──────────────────────────────────────────────────────────


def test_maxlen_negative_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        WorkingMemoryRing(maxlen=-1, lock=asyncio.Lock())


def test_maxlen_zero_disables_ring():
    ring = WorkingMemoryRing(maxlen=0, lock=asyncio.Lock())
    assert ring.enabled is False
    assert ring.maxlen == 0


def test_maxlen_positive_enables_ring():
    ring = WorkingMemoryRing(maxlen=5, lock=asyncio.Lock())
    assert ring.enabled is True
    assert ring.maxlen == 5


# ─── append / snapshot basics ──────────────────────────────────────────────


async def test_append_under_maxlen_preserves_all():
    ring = WorkingMemoryRing(maxlen=10, lock=asyncio.Lock())
    for i in range(3):
        await ring.append(_turn(i))
    snap = await ring.snapshot()
    assert [t.turn_id for t in snap] == ["t-0", "t-1", "t-2"]


async def test_append_over_maxlen_evicts_oldest():
    ring = WorkingMemoryRing(maxlen=3, lock=asyncio.Lock())
    for i in range(7):
        await ring.append(_turn(i))
    snap = await ring.snapshot()
    # Oldest 4 dropped; the freshest 3 remain in arrival order.
    assert [t.turn_id for t in snap] == ["t-4", "t-5", "t-6"]


async def test_append_when_disabled_is_noop():
    ring = WorkingMemoryRing(maxlen=0, lock=asyncio.Lock())
    for i in range(5):
        await ring.append(_turn(i))
    assert await ring.snapshot() == []


async def test_snapshot_empty_on_fresh_ring():
    ring = WorkingMemoryRing(maxlen=10, lock=asyncio.Lock())
    assert await ring.snapshot() == []


# ─── snapshot isolation (defensive copy) ───────────────────────────────────


async def test_snapshot_returns_copy_not_alias():
    ring = WorkingMemoryRing(maxlen=10, lock=asyncio.Lock())
    await ring.append(_turn(0))
    snap1 = await ring.snapshot()
    # Mutate the snapshot — ring must not see the change.
    snap1[0].user_text = "MUTATED"
    snap2 = await ring.snapshot()
    assert snap2[0].user_text == "user 0", (
        "snapshot returned alias; ring leaked internal state"
    )


async def test_snapshot_list_modification_does_not_affect_ring():
    ring = WorkingMemoryRing(maxlen=10, lock=asyncio.Lock())
    await ring.append(_turn(0))
    await ring.append(_turn(1))
    snap = await ring.snapshot()
    snap.pop()                                  # external list shrink
    assert len(await ring.snapshot()) == 2     # ring still has both


# ─── concurrency ───────────────────────────────────────────────────────────


async def test_concurrent_appends_serialised_no_loss():
    ring = WorkingMemoryRing(maxlen=200, lock=asyncio.Lock())
    await asyncio.gather(*[ring.append(_turn(i)) for i in range(100)])
    snap = await ring.snapshot()
    # All 100 appends land — under maxlen=200 nothing is evicted.
    assert len(snap) == 100
    # Order may interleave (gather is concurrent), but the set of turn_ids
    # must equal exactly the input set — no losses, no duplicates.
    assert {t.turn_id for t in snap} == {f"t-{i}" for i in range(100)}


async def test_shared_lock_does_not_deadlock_with_outer_critical_section():
    """Caller holding the lock for backend writes does NOT cause ring ops to
    block forever — the ring uses ``async with self._lock``, which is the
    same lock. ``asyncio.Lock`` does NOT recurse; the caller must release
    before invoking ring methods. Verify the documented usage works.
    """
    lock = asyncio.Lock()
    ring = WorkingMemoryRing(maxlen=5, lock=lock)
    async with lock:
        # Caller may NOT call ring.append here — would deadlock by contract.
        pass
    # After release, ring methods work normally.
    await ring.append(_turn(0))
    snap = await ring.snapshot()
    assert len(snap) == 1


# ─── clear ─────────────────────────────────────────────────────────────────


async def test_clear_drops_all_turns():
    ring = WorkingMemoryRing(maxlen=10, lock=asyncio.Lock())
    for i in range(5):
        await ring.append(_turn(i))
    await ring.clear()
    assert await ring.snapshot() == []


async def test_clear_on_disabled_ring_is_safe():
    ring = WorkingMemoryRing(maxlen=0, lock=asyncio.Lock())
    await ring.clear()
    assert await ring.snapshot() == []
