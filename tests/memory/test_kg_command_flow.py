"""T1-C: MCP tool publishes → worker process_command_message applies → KG (mocked NATS)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_memory_contracts import envelope_memory_payload, memory_command_subject

DLQ_SPACE = "default.alice.default"

SPACE = "default.alice.default"
SPACE_FOR_TESTS = SPACE
OTHER_SPACE = "default.bob.default"


@pytest.fixture
def kg_setup(tmp_path: Path):
    """Real KG + LockedKnowledgeGraph; mocked NATS via in-memory queue."""
    pytest.importorskip("mempalace")
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    db = tmp_path / "kg.sqlite3"
    locked = SqliteKnowledgeGraph(db, space_id=SPACE_FOR_TESTS, lock=asyncio.Lock())
    yield locked
    locked.close()


def _stub_msg(payload: dict, *, delivery: int = 1) -> SimpleNamespace:
    """Mimics a nats.aio.msg.Msg enough for process_command_message."""
    ack_calls = []
    nak_calls = []
    envelope = envelope_memory_payload(payload, kind=payload.get("kind", "memory_command"))

    async def _ack():
        ack_calls.append("ack")

    async def _nak():
        nak_calls.append("nak")

    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        subject=memory_command_subject(str(payload.get("memory_space_id") or SPACE)),
        ack=_ack,
        nak=_nak,
        ack_calls=ack_calls,
        nak_calls=nak_calls,
        metadata=SimpleNamespace(num_delivered=delivery),
    )


def test_command_dispatch_uses_only_the_sdk_wire_parser() -> None:
    import inspect

    from eidolon.memory.application import turn_processor

    source = inspect.getsource(turn_processor.process_command_message)
    assert "parse_memory_command(raw)" in source
    assert "unwrap_memory_payload" not in source


async def test_command_add_triple_flow(kg_setup, tmp_path: Path) -> None:
    """End-to-end inside the worker: a published KgAddTripleCommand applies to KG."""
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")

    msg = _stub_msg(
        {
            "kind": "kg_add_triple",
            "request_id": "r1",
            "memory_space_id": SPACE,
            "issued_at": "2026-05-19T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "tea",
        }
    )
    await process_command_message(
        msg,
        backend=None,
        kg=kg_setup,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )
    assert msg.ack_calls == ["ack"]

    records = await kg_setup.query_entity("alice", audiences=("owner",))
    assert any(r.predicate == "likes" and r.object == "tea" for r in records)
    status = await ledger.get("r1")
    assert status is not None
    assert status.status == "applied"
    assert status.resource_id


async def test_confirmed_privacy_command_deletes_exact_drawers(tmp_path: Path) -> None:
    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.domain.wire import MemoryWireRecord
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    backend = FakeMemoryBackend()
    for key in ("drawer_tea_1", "drawer_tea_2"):
        backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
            memory_space_id=SPACE,
            key=key,
            value="绿茶",
            metadata={"memory_space_id": SPACE, "wing": "Wing_Profile"},
        )
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")
    msg = _stub_msg(
        {
            "kind": "privacy_mutation",
            "request_id": "privacy-1",
            "memory_space_id": SPACE,
            "issued_at": "2026-05-19T10:00:00Z",
            "action": "delete",
            "drawer_ids": ["drawer_tea_1", "drawer_tea_2"],
            "preview_id": "preview-1",
            "target": "绿茶",
        }
    )

    await process_command_message(
        msg,
        backend=backend,
        kg=None,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )

    assert msg.ack_calls == ["ack"]
    assert await backend.get(SPACE, "drawer_tea_1") is None
    assert await backend.get(SPACE, "drawer_tea_2") is None
    status = await ledger.get("privacy-1")
    assert status is not None
    assert status.status == "applied"
    assert status.resource_id == "delete:2:preview-1"


async def test_command_failure_naks_and_reports_retrying(tmp_path: Path) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    class _BrokenKg:
        async def add_triple(self, **_kwargs):
            raise RuntimeError("temporary KG outage")

    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")
    msg = _stub_msg(
        {
            "kind": "kg_add_triple",
            "request_id": "retry-1",
            "memory_space_id": SPACE,
            "issued_at": "2026-05-19T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "tea",
        }
    )

    await process_command_message(
        msg,
        backend=None,
        kg=_BrokenKg(),
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )

    assert msg.ack_calls == []
    assert msg.nak_calls == ["nak"]
    status = await ledger.get("retry-1")
    assert status is not None
    assert status.status == "retrying"
    assert "temporary KG outage" in (status.error or "")


async def test_command_terminal_failure_is_dlq_and_truthfully_failed(tmp_path: Path) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger
    from eidolon.memory.infrastructure.dlq import DlqLedger

    class _BrokenKg:
        async def add_triple(self, **_kwargs):
            raise RuntimeError("permanent KG outage")

    settings = load_memory_settings().model_copy(deep=True)
    settings.nats.worker_max_deliveries = 3
    settings.nats.dlq_log_path = str(tmp_path / "command_dlq.jsonl")
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")
    dlq = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)
    msg = _stub_msg(
        {
            "kind": "kg_add_triple",
            "request_id": "failed-1",
            "memory_space_id": SPACE,
            "issued_at": "2026-05-19T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "tea",
        },
        delivery=3,
    )

    await process_command_message(
        msg,
        backend=None,
        kg=_BrokenKg(),
        settings=settings,
        expected_memory_space_id=SPACE,
        command_status=ledger,
        dlq_writer=dlq,
    )

    assert msg.ack_calls == ["ack"]
    assert msg.nak_calls == []
    status = await ledger.get("failed-1")
    assert status is not None
    assert status.status == "failed"
    assert "permanent KG outage" in (status.error or "")
    assert not (tmp_path / "command_dlq.jsonl").exists()
    dead_letters = await dlq.list(state="unresolved")
    assert len(dead_letters) == 1
    assert dead_letters[0].subject == memory_command_subject(SPACE)
    assert dead_letters[0].payload_size > 0


async def test_command_invalidate_flow(kg_setup) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings

    await kg_setup.add_triple(
        audience="owner",
        subject="alice", predicate="likes", object="coffee",
        source_turn_id="seed", adapter_name="test",
    )

    msg = _stub_msg(
        {
            "kind": "kg_invalidate",
            "request_id": "r2",
            "memory_space_id": SPACE,
            "issued_at": "2026-05-19T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "coffee",
            "ended": "2026-05-19T11:00:00Z",
        }
    )
    await process_command_message(
        msg,
        backend=None,
        kg=kg_setup,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
    )
    assert msg.ack_calls == ["ack"]
    applied = await kg_setup.find_invalidation_applied(
        "alice", "likes", "coffee", "2026-05-19T11:00:00Z"
    )
    assert applied


async def test_command_invalidate_missing_triple_retries(kg_setup, tmp_path: Path) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.command_status import CommandStatusLedger

    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")
    msg = _stub_msg(
        {
            "kind": "kg_invalidate",
            "request_id": "missing-invalidate",
            "memory_space_id": SPACE,
            "issued_at": "2026-05-19T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "missing",
            "ended": "2026-05-19T11:00:00Z",
        }
    )

    await process_command_message(
        msg,
        backend=None,
        kg=kg_setup,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )

    assert msg.ack_calls == []
    assert msg.nak_calls == ["nak"]
    status = await ledger.get("missing-invalidate")
    assert status is not None
    assert status.status == "retrying"
    assert status.attempts == 1


async def test_command_bad_payload_acked_not_raised(kg_setup) -> None:
    """Invalid JSON or schema → ack (don't pump DLQ for admin commands)."""
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings

    msg = SimpleNamespace(
        data=b"not json",
        ack=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )
    await process_command_message(
        msg,
        backend=None,
        kg=kg_setup,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
    )
    assert msg.ack.await_count == 1


async def test_command_bad_payload_is_inspectable_in_production_dlq(
    kg_setup, tmp_path: Path
) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.dlq import DlqLedger

    msg = SimpleNamespace(
        data=b"not json",
        subject=memory_command_subject(SPACE),
        ack=AsyncMock(),
        metadata=SimpleNamespace(num_delivered=1),
    )
    dlq = DlqLedger(tmp_path / "dlq.sqlite3", space_id=DLQ_SPACE)

    await process_command_message(
        msg,
        backend=None,
        kg=kg_setup,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
        dlq_writer=dlq,
    )

    assert msg.ack.await_count == 1
    records = await dlq.list(state="unresolved")
    assert len(records) == 1
    assert records[0].subject == memory_command_subject(SPACE)
    assert "invalid command payload" in records[0].error


async def test_command_user_id_mismatch_acked(kg_setup) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings

    msg = _stub_msg(
        {
            "kind": "kg_add_triple",
            "request_id": "x",
            "memory_space_id": OTHER_SPACE,          # mismatch
            "issued_at": "2026-05-19T10:00:00Z",
            "subject": "self",
            "predicate": "likes",
            "object": "x",
        }
    )
    await process_command_message(
        msg,
        backend=None,
        kg=kg_setup,
        settings=get_memory_settings(),
        expected_memory_space_id=SPACE,
    )
    assert msg.ack_calls == ["ack"]
    # Did not apply to KG
    records = await kg_setup.query_entity("self", audiences=("owner",))
    assert not any(r.predicate == "likes" for r in records)


async def test_command_unknown_kind_acked(kg_setup) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings

    msg = _stub_msg({"kind": "not_a_real_kind", "request_id": "?"})
    await process_command_message(
        msg, backend=None, kg=kg_setup,
        settings=get_memory_settings(), expected_memory_space_id=SPACE,
    )
    assert msg.ack_calls == ["ack"]


async def test_subject_helpers() -> None:
    from eidolon_memory_contracts import (
        all_memory_stream_patterns,
        memory_command_subject,
    )

    assert memory_command_subject("default.alice.default") == (
        "eidolon.memory.cmd.b64_ZGVmYXVsdC5hbGljZS5kZWZhdWx0"
    )
    patterns = all_memory_stream_patterns()
    assert "eidolon.memory.turn.*" in patterns
    assert "eidolon.memory.cmd.*" in patterns
    assert "eidolon.memory.sync.*" in patterns
