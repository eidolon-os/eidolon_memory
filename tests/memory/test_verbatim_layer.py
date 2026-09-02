"""The turn survives everything the steward can do to it.

Measured on the 48-query battery: six queries are answerable only from the
user's own sentence and six only from what the steward distilled. Before this
layer the palace held one of those halves, and a turn the steward rejected left
nothing behind at all — the decision ledger keeps a hash, not the text.

So the property under test is not "recall improved". It is that a turn is
written before anything can reject it, and that keeping it does not grow
without bound.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eidolon_memory_contracts import envelope_memory_payload

from eidolon.memory.application.verbatim import (
    VERBATIM_SOURCE,
    prune_verbatim,
    verbatim_drawer,
)

MEMORY_SPACE_ID = "r:alice:default"


@pytest.fixture
def settings():
    from eidolon.memory.config.memory_settings import load_memory_settings

    return load_memory_settings()


@pytest.fixture
def backend():
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.adapters.locked_backend import LockedBackend

    return LockedBackend(FakeMemoryBackend())


def _payload(**kwargs) -> dict:
    return {
        "turn_id": kwargs.get("turn_id") or uuid.uuid4().hex,
        "context": {
            "owner_id": "alice",
            "companion_id": "test",
            "memory_realm_id": MEMORY_SPACE_ID,
            "device_id": "device",
            "session_id": "s1",
        },
        "timestamp": "2026-05-19T10:00:00Z",
        "user_text": kwargs.get("user_text", "我妈张丽最近又失眠了"),
        "assistant_text": kwargs.get("assistant_text", "听起来你很担心她。"),
    }


def _msg(payload: dict) -> SimpleNamespace:
    acks: list[str] = []
    naks: list[str] = []
    envelope = envelope_memory_payload(payload, kind="conversation_turn")
    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        ack=lambda: _append(acks, "ack"),
        nak=lambda: _append(naks, "nak"),
        ack_calls=acks,
        nak_calls=naks,
        metadata=SimpleNamespace(num_delivered=1),
    )


async def _append(target: list[str], value: str) -> None:
    target.append(value)


def _steward_that_fails():
    steward = MagicMock()
    steward.extraction_version = "test:v1"
    steward.decide = AsyncMock(side_effect=RuntimeError("model unreachable"))
    return steward


async def test_the_sentence_survives_a_steward_that_refuses_the_turn(settings, backend) -> None:
    """The case the layer exists for.

    Four separate blank-field defects this month discarded a whole turn's
    extraction. None of them should have cost the sentence, and before this
    layer every one of them did.
    """
    from eidolon.memory.application.turn_processor import process_turn_message

    payload = _payload(user_text="我最近开始追一档播客，名字叫海棠播客")
    await process_turn_message(
        _msg(payload),
        steward=_steward_that_fails(),
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
    )

    rows = await backend.get_all(MEMORY_SPACE_ID)
    verbatim = [r for r in rows if (r.metadata or {}).get("source") == VERBATIM_SOURCE]
    assert len(verbatim) == 1
    assert "海棠播客" in (verbatim[0].value or "")


async def test_the_assistant_half_is_not_stored(settings, backend) -> None:
    """Measured worse — 31 of 43 against 32 — and it is model prose besides."""
    from eidolon.memory.application.turn_processor import process_turn_message

    await process_turn_message(
        _msg(_payload(user_text="我喜欢乌龙茶", assistant_text="我会记住你偏好乌龙茶。")),
        steward=_steward_that_fails(),
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
    )

    rows = await backend.get_all(MEMORY_SPACE_ID)
    verbatim = next(r for r in rows if (r.metadata or {}).get("source") == VERBATIM_SOURCE)
    assert "乌龙茶" in (verbatim.value or "")
    assert "我会记住" not in (verbatim.value or "")


async def test_the_drawer_carries_the_source_event_the_forget_path_resolves_by(
    settings, backend
) -> None:
    """It needs no second delete route — privacy already resolves by exact event."""
    from eidolon.memory.application.turn_processor import process_turn_message

    payload = _payload()
    await process_turn_message(
        _msg(payload),
        steward=_steward_that_fails(),
        backend=backend,
        settings=settings,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
    )

    rows = await backend.get_all(MEMORY_SPACE_ID)
    verbatim = next(r for r in rows if (r.metadata or {}).get("source") == VERBATIM_SOURCE)
    assert (verbatim.metadata or {}).get("source_event_id") == payload["turn_id"]


async def test_disabling_retention_disables_the_layer(settings, backend) -> None:
    """0 is the pre-2026-09-02 behaviour, reachable by configuration alone."""
    from eidolon.memory.application.turn_processor import process_turn_message

    off = settings.model_copy(
        update={"worker": settings.worker.model_copy(update={"verbatim_retention_days": 0})}
    )
    await process_turn_message(
        _msg(_payload()),
        steward=_steward_that_fails(),
        backend=backend,
        settings=off,
        max_deliveries=3,
        expected_memory_space_id=MEMORY_SPACE_ID,
    )

    rows = await backend.get_all(MEMORY_SPACE_ID)
    assert not [r for r in rows if (r.metadata or {}).get("source") == VERBATIM_SOURCE]


async def test_an_empty_user_turn_files_nothing() -> None:
    """A turn with nothing said is not evidence of anything."""
    from eidolon_memory_contracts import ConversationTurnPayload

    turn = ConversationTurnPayload.model_validate(_payload(user_text="   "))

    assert verbatim_drawer(turn) is None


async def test_the_sentence_stays_on_the_device_that_heard_it() -> None:
    """Guessing the other way leaked, and a multidevice E2E caught it.

    Scope is content-dependent and the steward decides it — "这台设备在客厅，
    麦克风需要校准" is device-local and "我喜欢乌龙茶" is not — but the steward
    has not run when this drawer is written. The two guesses are not
    symmetric: all_devices sends a device-local sentence to every device,
    current_device only under-serves. It is also the truer description, since
    what crosses an owner's devices is the fact distilled from the turn, and
    that projection carries its own scope.
    """
    from eidolon_memory_contracts import ConversationTurnPayload

    drawer = verbatim_drawer(
        ConversationTurnPayload.model_validate(_payload(user_text="这台设备在客厅，麦克风需要校准"))
    )

    assert drawer is not None
    assert drawer.visibility == "current_device"
    assert drawer.scope == "session"


# ── the bound ────────────────────────────────────────────────────────────────


def _row(key: str, *, source: str, occurred_at: str | None):
    meta = {"source": source}
    if occurred_at is not None:
        meta["occurred_at"] = occurred_at
    return SimpleNamespace(key=key, value="x", metadata=meta)


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.deleted: list[str] = []

    async def get_all(self, space, limit=None, offset=0):
        return self.rows

    async def delete_many(self, space, keys):
        self.deleted.extend(keys)
        return list(keys)


async def test_pruning_drops_the_expired_and_leaves_everything_else() -> None:
    store = _Store(
        [
            _row("old", source=VERBATIM_SOURCE, occurred_at="2020-01-01T00:00:00+00:00"),
            _row("new", source=VERBATIM_SOURCE, occurred_at="2999-01-01T00:00:00+00:00"),
            # A distilled fact is not this layer's to expire, however old.
            _row("fact", source="canonical-natural", occurred_at="2020-01-01T00:00:00+00:00"),
        ]
    )

    deleted = await prune_verbatim(store, "s", retention_days=30, max_records=1000)

    assert deleted == 1
    assert store.deleted == ["old"]


async def test_an_undated_drawer_is_kept() -> None:
    """An unreadable date must not be indistinguishable from an old one."""
    store = _Store([_row("undated", source=VERBATIM_SOURCE, occurred_at=None)])

    assert await prune_verbatim(store, "s", retention_days=1, max_records=1000) == 0
    assert store.deleted == []


async def test_overflow_trims_the_oldest_first() -> None:
    """The window is the policy; max_records is the backstop for a burst."""
    rows = [
        _row(f"t{i}", source=VERBATIM_SOURCE, occurred_at=f"2999-01-{i:02d}T00:00:00+00:00")
        for i in range(1, 6)
    ]
    store = _Store(rows)

    # Far enough out that nothing expires, so only the overflow rule can act.
    deleted = await prune_verbatim(store, "s", retention_days=36_500, max_records=3)

    assert deleted == 2
    assert store.deleted == ["t1", "t2"]


async def test_an_absurd_retention_does_not_raise_into_the_turn() -> None:
    """The setting bounds itself from below only, and the promise is no raise.

    A million days overflowed the cutoff subtraction, which would have taken
    down the turn whose sentence this layer exists to keep.
    """
    store = _Store([_row("t", source=VERBATIM_SOURCE, occurred_at="2999-01-01T00:00:00+00:00")])

    assert await prune_verbatim(store, "s", retention_days=10**9, max_records=10) == 0


async def test_a_failing_store_does_not_take_the_turn_down() -> None:
    """Losing a pruning pass costs disk; raising here would cost the guarantee."""

    class _Broken(_Store):
        async def get_all(self, space, limit=None, offset=0):
            raise RuntimeError("chroma unavailable")

    assert await prune_verbatim(_Broken([]), "s", retention_days=30, max_records=10) == 0
