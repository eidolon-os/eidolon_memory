"""T1-C: MCP tool publishes → worker process_command_message applies → KG (mocked NATS)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.memory import envelope_memory_payload

SPACE = "default.alice.default"
OTHER_SPACE = "default.bob.default"


@pytest.fixture
def kg_setup(tmp_path: Path):
    """Real KG + LockedKnowledgeGraph; mocked NATS via in-memory queue."""
    pytest.importorskip("mempalace")
    from mempalace.knowledge_graph import KnowledgeGraph

    from eidolon.memory.adapters.locked_kg import LockedKnowledgeGraph

    db = tmp_path / "kg.sqlite3"
    locked = LockedKnowledgeGraph(KnowledgeGraph(db_path=str(db)), asyncio.Lock())
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

    records = await kg_setup.query_entity("alice")
    assert any(r.predicate == "likes" and r.object == "tea" for r in records)
    status = await ledger.get("r1")
    assert status is not None
    assert status.status == "applied"
    assert status.resource_id


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

    class _BrokenKg:
        async def add_triple(self, **_kwargs):
            raise RuntimeError("permanent KG outage")

    settings = load_memory_settings().model_copy(deep=True)
    settings.nats.worker_max_deliveries = 3
    settings.nats.dlq_log_path = str(tmp_path / "command_dlq.jsonl")
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3")
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
    )

    assert msg.ack_calls == ["ack"]
    assert msg.nak_calls == []
    status = await ledger.get("failed-1")
    assert status is not None
    assert status.status == "failed"
    assert "permanent KG outage" in (status.error or "")
    assert (tmp_path / "command_dlq.jsonl").is_file()


async def test_command_invalidate_flow(kg_setup) -> None:
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings

    await kg_setup.add_triple(
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
    records = await kg_setup.query_entity("self")
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
    from eidolon_sdk.memory import (
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
