from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eidolon.memory.infrastructure.command_status import CommandStatusLedger

CMD_SPACE = "default.alice.default"


async def test_command_status_never_downgrades_terminal_state(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3", space_id=CMD_SPACE)

    await ledger.record_applied("req-1", kind="memory_intent", resource_id="drawer-1")
    await ledger.record_accepted("req-1", kind="memory_intent")
    await ledger.record_failed("req-1", kind="memory_intent", error="late duplicate")

    record = await ledger.get("req-1")
    assert record is not None
    assert record.status == "applied"
    assert record.resource_id == "drawer-1"
    assert record.error is None
    assert record.attempts == 1


async def test_command_status_stats_expose_capacity_and_active_work(tmp_path: Path) -> None:
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    ledger = CommandStatusLedger(
        tmp_path / "command_status.sqlite3",
        space_id=CMD_SPACE,
        retention_days=7,
        max_records=123,
    )
    await ledger.record_accepted("a", kind="test")
    await ledger.record_retrying("b", kind="test", error="temporary")
    await ledger.record_applied("c", kind="test")

    stats = await ledger.stats()

    assert stats.total == 3
    assert stats.accepted == 1
    assert stats.retrying == 1
    assert stats.applied == 1
    assert stats.retention_days == 7
    assert stats.max_records == 123
    assert stats.database_bytes > 0
    assert stats.oldest_active_at is not None


async def test_retrying_can_recover_to_applied(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3", space_id=CMD_SPACE)

    await ledger.record_accepted("req-2", kind="kg_add_triple")
    await ledger.record_retrying("req-2", kind="kg_add_triple", error="temporary")
    retrying = await ledger.get("req-2")
    assert retrying is not None
    assert retrying.status == "retrying"
    assert retrying.attempts == 1

    await ledger.record_applied("req-2", kind="kg_add_triple", resource_id="triple-2")
    applied = await ledger.get("req-2")
    assert applied is not None
    assert applied.status == "applied"
    assert applied.error is None
    assert applied.resource_id == "triple-2"
    assert applied.attempts == 2


async def test_wait_terminal_reads_without_backend_lock(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3", space_id=CMD_SPACE)
    backend_lock = asyncio.Lock()
    await backend_lock.acquire()
    try:
        await ledger.record_accepted("req-3", kind="memory_intent")

        async def _complete() -> None:
            await asyncio.sleep(0.01)
            await ledger.record_applied("req-3", kind="memory_intent")

        task = asyncio.create_task(_complete())
        record = await ledger.wait_terminal("req-3", timeout_seconds=0.2)
        await task
    finally:
        backend_lock.release()

    assert record is not None
    assert record.status == "applied"


async def test_status_survives_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "command_status.sqlite3"
    first_process = CommandStatusLedger(path, space_id=CMD_SPACE)
    await first_process.record_applied(
        "req-restart",
        kind="memory_forget",
        resource_id="deleted:2",
    )

    restarted_process = CommandStatusLedger(path, space_id=CMD_SPACE)
    record = await restarted_process.get("req-restart")

    assert record is not None
    assert record.status == "applied"
    assert record.resource_id == "deleted:2"


def test_status_ledger_implements_ports_without_application_infrastructure_import(
    tmp_path: Path,
) -> None:
    import inspect

    from eidolon.memory.application import turn_processor
    from eidolon.memory.domain.ports import (
        CommandStatusReader,
        CommandStatusStore,
        CommandStatusWriter,
    )

    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3", space_id=CMD_SPACE)
    assert isinstance(ledger, CommandStatusReader)
    assert isinstance(ledger, CommandStatusWriter)
    assert isinstance(ledger, CommandStatusStore)
    assert "infrastructure.command_status" not in inspect.getsource(turn_processor)


async def test_prune_expires_only_terminal_rows(tmp_path: Path) -> None:
    path = tmp_path / "command_status.sqlite3"
    ledger = CommandStatusLedger(path, retention_days=1, space_id=CMD_SPACE)
    await ledger.record_applied("old-applied", kind="memory_intent")
    await ledger.record_failed("old-failed", kind="kg_add_triple", error="terminal")
    await ledger.record_accepted("old-active", kind="memory_intent")
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE command_status SET updated_at = ?", (old,))

    assert await ledger.prune() == 2
    assert await ledger.get("old-applied") is None
    assert await ledger.get("old-failed") is None
    assert await ledger.get("old-active") is not None


async def test_prune_caps_terminal_history_but_preserves_active_rows(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(
        tmp_path / "command_status.sqlite3",
        space_id=CMD_SPACE,
        retention_days=365,
        max_records=2,
        prune_every_writes=100,
    )
    await ledger.record_applied("terminal-1", kind="memory_intent")
    await ledger.record_applied("terminal-2", kind="memory_intent")
    await ledger.record_applied("terminal-3", kind="memory_intent")
    await ledger.record_accepted("active", kind="memory_intent")

    assert await ledger.prune() == 2
    assert await ledger.get("active") is not None
    remaining_terminal = [
        request_id
        for request_id in ("terminal-1", "terminal-2", "terminal-3")
        if await ledger.get(request_id) is not None
    ]
    assert len(remaining_terminal) == 1


async def test_periodic_prune_keeps_projection_bounded(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(
        tmp_path / "command_status.sqlite3",
        space_id=CMD_SPACE,
        retention_days=365,
        max_records=2,
        prune_every_writes=1,
    )

    for index in range(5):
        await ledger.record_applied(f"req-{index}", kind="memory_intent")

    retained = [
        request_id
        for request_id in (f"req-{index}" for index in range(5))
        if await ledger.get(request_id) is not None
    ]
    assert len(retained) == 2
