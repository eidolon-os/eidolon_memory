from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.dlq import DlqLedger

DLQ_SPACE = "default.alice.default"


async def test_dlq_persists_full_payload_but_exposes_only_bounded_preview(
    tmp_path: Path,
) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)
    payload = ("敏感内容" * 200).encode()

    record = await ledger.add(
        subject="eidolon.memory.cmd.test",
        payload=payload,
        error="boom",
        deliveries=3,
    )

    assert record.payload_size == len(payload)
    assert len(record.payload_preview.encode()) <= 502
    assert "payload" not in record.to_dict()
    restarted = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)
    stored = await restarted.get(record.entry_id)
    assert stored == record


async def test_dlq_replay_claim_is_atomic_and_duplicate_safe(tmp_path: Path) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)
    record = await ledger.add(
        subject="eidolon.memory.cmd.test",
        payload=b'{"kind":"test"}',
        error="boom",
        deliveries=3,
    )

    first, second = await asyncio.gather(
        ledger.claim_replay(record.entry_id),
        ledger.claim_replay(record.entry_id),
    )

    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    assert claims[0].payload == b'{"kind":"test"}'
    replayed = await ledger.mark_replayed(record.entry_id)
    assert replayed.state == "replayed"
    assert replayed.replay_attempts == 1
    assert await ledger.claim_replay(record.entry_id) is None


async def test_dlq_failed_replay_can_retry_and_resolve(tmp_path: Path) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)
    record = await ledger.add(
        subject="eidolon.memory.turn.test",
        payload=b"payload",
        error="original",
        deliveries=3,
    )
    assert await ledger.claim_replay(record.entry_id) is not None
    released = await ledger.release_replay(record.entry_id, error="NATS unavailable")
    assert released.state == "unresolved"
    assert released.replay_attempts == 1

    resolved = await ledger.resolve(record.entry_id, note="invalid legacy payload")
    assert resolved.state == "resolved"
    assert resolved.resolution_note == "invalid legacy payload"
    assert await ledger.claim_replay(record.entry_id) is None

    stats = await ledger.stats()
    assert stats.total == 1
    assert stats.resolved == 1
    assert stats.unresolved == 0
    assert stats.payload_bytes == len(b"payload")


async def test_dlq_replaying_claim_recovers_after_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "dlq.sqlite3"
    ledger = DlqLedger(path, space_id=DLQ_SPACE)
    record = await ledger.add(
        subject="eidolon.memory.cmd.test",
        payload=b"payload",
        error="boom",
        deliveries=3,
    )
    assert await ledger.claim_replay(record.entry_id) is not None

    restarted = DlqLedger(path, space_id=DLQ_SPACE)
    recovered = await restarted.get(record.entry_id)
    assert recovered is not None
    assert recovered.state == "unresolved"
    assert recovered.replay_attempts == 1


async def test_dlq_rejects_invalid_operations(tmp_path: Path) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)
    with pytest.raises(ValueError, match="invalid DLQ state"):
        await ledger.list(state="anything")
    with pytest.raises(ValueError, match="note"):
        await ledger.resolve("missing", note="")


async def test_capacity_is_atomic_and_preserves_resolved_payload(tmp_path: Path) -> None:
    from eidolon.memory.domain.dlq import DlqCapacityExceeded

    path = tmp_path / "quota.sqlite3"
    ledgers = [DlqLedger(path, space_id=DLQ_SPACE, max_records=1) for _ in range(2)]

    async def add(ledger):
        return await ledger.add(subject="test", payload=b"original", error="error", deliveries=3)

    outcomes = await asyncio.gather(*(add(x) for x in ledgers), return_exceptions=True)
    assert sum(isinstance(x, DlqCapacityExceeded) for x in outcomes) == 1
    record = (await ledgers[0].list())[0]
    await ledgers[0].resolve(record.entry_id, note="reviewed")
    with pytest.raises(DlqCapacityExceeded):
        await add(ledgers[1])
    assert (await ledgers[0].get(record.entry_id)).payload_preview == "original"
    assert (await ledgers[0].stats()).total == 1
    # Explicit capacity adjustment can admit new rows without erasing old ones.
    expanded = DlqLedger(path, space_id=DLQ_SPACE, max_records=2)
    await add(expanded)
    assert (await expanded.stats()).total == 2


async def test_payload_budget_is_per_space_and_lowering_keeps_existing(tmp_path: Path) -> None:
    from eidolon.memory.domain.dlq import DlqCapacityExceeded

    path = tmp_path / "bytes.sqlite3"
    ledger = DlqLedger(path, space_id=DLQ_SPACE, max_payload_bytes=6)
    kwargs = dict(subject="test", payload="你好".encode(), error="error", deliveries=1)
    record = await ledger.add(**kwargs)
    with pytest.raises(DlqCapacityExceeded):
        await ledger.add(**kwargs)
    other = DlqLedger(path, space_id="other", max_payload_bytes=6)
    await other.add(**kwargs)
    lowered = DlqLedger(path, space_id=DLQ_SPACE, max_payload_bytes=1)
    claim = await lowered.claim_replay(record.entry_id)
    assert claim.payload == kwargs["payload"]
    await lowered.mark_replayed(record.entry_id)
    assert (await lowered.stats()).payload_bytes == 6
    with pytest.raises(DlqCapacityExceeded):
        await lowered.add(**kwargs)


async def test_full_dlq_does_not_ack_rejected_turn(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from eidolon.memory.application.turn_processor import process_turn_message
    from eidolon.memory.config.memory_settings import MemorySettings
    from eidolon.memory.domain.dlq import DlqCapacityExceeded

    ledger = DlqLedger(tmp_path / "full.sqlite3", space_id=DLQ_SPACE, max_records=1)
    await ledger.add(subject="original", payload=b"keep", error="failed", deliveries=3)
    msg = SimpleNamespace(
        data=b"invalid-json", subject="memory.turn", ack=AsyncMock(), nak=AsyncMock()
    )
    with pytest.raises(DlqCapacityExceeded):
        await process_turn_message(
            msg,
            steward=None,
            backend=None,
            settings=MemorySettings(),
            max_deliveries=3,
            dlq_writer=ledger,
        )
    msg.ack.assert_not_awaited()
    assert (await ledger.stats()).total == 1


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
def test_dlq_capacity_configuration_rejects_invalid_limits(value) -> None:
    from pydantic import ValidationError

    from eidolon.memory.config.memory_settings import DlqCapacityConfig

    for field in ("max_records", "max_payload_bytes"):
        with pytest.raises(ValidationError):
            DlqCapacityConfig(**{field: value})
