from __future__ import annotations

import asyncio
from pathlib import Path

from eidolon.memory.infrastructure.command_status import CommandStatusLedger


async def test_command_status_never_downgrades_terminal_state(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")

    await ledger.record_applied("req-1", kind="user_confirm_fact", resource_id="drawer-1")
    await ledger.record_accepted("req-1", kind="user_confirm_fact")
    await ledger.record_failed("req-1", kind="user_confirm_fact", error="late duplicate")

    record = await ledger.get("req-1")
    assert record is not None
    assert record.status == "applied"
    assert record.resource_id == "drawer-1"
    assert record.error is None
    assert record.attempts == 1


async def test_retrying_can_recover_to_applied(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")

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
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")
    backend_lock = asyncio.Lock()
    await backend_lock.acquire()
    try:
        await ledger.record_accepted("req-3", kind="user_confirm_fact")

        async def _complete() -> None:
            await asyncio.sleep(0.01)
            await ledger.record_applied("req-3", kind="user_confirm_fact")

        task = asyncio.create_task(_complete())
        record = await ledger.wait_terminal("req-3", timeout_seconds=0.2)
        await task
    finally:
        backend_lock.release()

    assert record is not None
    assert record.status == "applied"


async def test_status_survives_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "command_status.sqlite3"
    first_process = CommandStatusLedger(path)
    await first_process.record_applied(
        "req-restart",
        kind="memory_forget",
        resource_id="deleted:2",
    )

    restarted_process = CommandStatusLedger(path)
    record = await restarted_process.get("req-restart")

    assert record is not None
    assert record.status == "applied"
    assert record.resource_id == "deleted:2"
