from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.dlq import DlqLedger


async def test_dlq_persists_full_payload_but_exposes_only_bounded_preview(
    tmp_path: Path,
) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3")
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
    restarted = DlqLedger(tmp_path / "dlq.sqlite3")
    stored = await restarted.get(record.entry_id)
    assert stored == record


async def test_dlq_replay_claim_is_atomic_and_duplicate_safe(tmp_path: Path) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3")
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
    ledger = DlqLedger(tmp_path / "dlq.sqlite3")
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
    ledger = DlqLedger(path)
    record = await ledger.add(
        subject="eidolon.memory.cmd.test",
        payload=b"payload",
        error="boom",
        deliveries=3,
    )
    assert await ledger.claim_replay(record.entry_id) is not None

    restarted = DlqLedger(path)
    recovered = await restarted.get(record.entry_id)
    assert recovered is not None
    assert recovered.state == "unresolved"
    assert recovered.replay_attempts == 1


async def test_dlq_rejects_invalid_operations(tmp_path: Path) -> None:
    ledger = DlqLedger(tmp_path / "dlq.sqlite3")
    with pytest.raises(ValueError, match="invalid DLQ state"):
        await ledger.list(state="anything")
    with pytest.raises(ValueError, match="note"):
        await ledger.resolve("missing", note="")
