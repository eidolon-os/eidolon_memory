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


def _stub_msg(payload: dict) -> SimpleNamespace:
    """Mimics a nats.aio.msg.Msg enough for process_command_message."""
    ack_calls = []
    envelope = envelope_memory_payload(payload, kind=payload.get("kind", "memory_command"))

    async def _ack():
        ack_calls.append("ack")

    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        ack=_ack,
        ack_calls=ack_calls,
        metadata=SimpleNamespace(num_delivered=1),
    )


async def test_command_add_triple_flow(kg_setup) -> None:
    """End-to-end inside the worker: a published KgAddTripleCommand applies to KG."""
    from eidolon.memory.application.turn_processor import process_command_message
    from eidolon.memory.config.memory_settings import get_memory_settings

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
    )
    assert msg.ack_calls == ["ack"]

    records = await kg_setup.query_entity("alice")
    assert any(r.predicate == "likes" and r.object == "tea" for r in records)


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
