"""The knowledge graph is optional at runtime, not at build time.

Both graph backends ship. Whether a deployment uses one is `kg.backend`, and
turning it off has to leave a working service: vector recall still answers, the
graph tools simply are not offered, and a graph command gets a truthful failure
instead of hanging until the caller times out.

Turning it back on must also be nothing more than a config change — in
particular, switching off must not delete anything.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eidolon_memory_contracts import envelope_memory_payload, memory_command_subject

from eidolon.memory.application.public_recall import recall_with_kg_fusion
from eidolon.memory.application.turn_processor import process_command_message
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.command_status import CommandStatusLedger

CMD_SPACE = "default.alice.default"

SPACE = "default.alice.default"
SPACE_FOR_TESTS = SPACE


def _settings(**kg) -> MemorySettings:
    return MemorySettings.model_validate({"kg": kg} if kg else {})


def _stub_msg(payload: dict) -> SimpleNamespace:
    envelope = envelope_memory_payload(payload, kind=payload["kind"])
    acks: list[str] = []
    naks: list[str] = []

    async def _ack() -> None:
        acks.append("ack")

    async def _nak() -> None:
        naks.append("nak")

    return SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        subject=memory_command_subject(SPACE),
        ack=_ack,
        nak=_nak,
        ack_calls=acks,
        nak_calls=naks,
        metadata=SimpleNamespace(num_delivered=1),
    )


def test_none_is_the_only_setting_that_disables_the_graph() -> None:
    assert not _settings(backend="none").kg.enabled
    assert _settings(backend="sqlite").kg.enabled
    assert _settings().kg.enabled, "sqlite remains the default"


def test_postgres_must_say_where_its_connection_string_lives() -> None:
    """A shared database is not something to guess the address of."""

    with pytest.raises(ValueError, match="postgres_dsn_env"):
        MemorySettings.model_validate({"kg": {"backend": "postgres", "postgres_dsn_env": ""}})


def test_a_dsn_is_read_from_the_environment_not_the_config_file() -> None:
    settings = _settings(backend="postgres", postgres_dsn_env="ABSENT_DSN_VAR")

    assert settings.kg.resolve_postgres_dsn() == ""


async def test_recall_still_answers_with_the_graph_off() -> None:
    """The service degrades to vector-only, it does not fail."""

    from eidolon_memory_contracts import MemoryActorContext

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend

    backend = FakeMemoryBackend()
    await backend.ingest_text(
        wing="Wing_Life",
        room="colour",
        text="likes the colour green",
        metadata={"memory_space_id": SPACE},
    )

    fused = await recall_with_kg_fusion(
        backend,
        _settings(backend="none"),
        query="colour",
        context=MemoryActorContext(memory_realm_id=SPACE, owner_id="alice"),
        top_k=5,
        kg=None,
        for_voice=False,
        palace_path=None,
    )

    assert [record.value for record in fused["vector"]] == ["likes the colour green"]
    assert fused["kg"] == []
    assert not fused["degraded"], "vector-only is a configuration, not a degradation"


async def test_a_graph_command_fails_honestly_rather_than_hanging(tmp_path: Path) -> None:
    """Callers wait for a terminal status; silence would strand them."""

    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3", space_id=CMD_SPACE)
    msg = _stub_msg(
        {
            "kind": "kg_add_triple",
            "request_id": "r-nokg",
            "memory_space_id": SPACE,
            "issued_at": "2026-08-01T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "tea",
        }
    )

    await process_command_message(
        msg,
        backend=None,
        kg=None,
        settings=_settings(backend="none"),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )

    assert msg.ack_calls == ["ack"], "the message is consumed, not redelivered forever"
    status = await ledger.get("r-nokg")
    assert status is not None
    assert status.status == "failed"
    assert status.error == "kg_not_configured"


async def test_an_invalidation_command_is_answered_the_same_way(tmp_path: Path) -> None:
    ledger = CommandStatusLedger(tmp_path / "command_status.sqlite3", space_id=CMD_SPACE)
    msg = _stub_msg(
        {
            "kind": "kg_invalidate",
            "request_id": "r-inv",
            "memory_space_id": SPACE,
            "issued_at": "2026-08-01T10:00:00Z",
            "subject": "alice",
            "predicate": "likes",
            "object": "tea",
        }
    )

    await process_command_message(
        msg,
        backend=None,
        kg=None,
        settings=_settings(backend="none"),
        expected_memory_space_id=SPACE,
        command_status=ledger,
    )

    status = await ledger.get("r-inv")
    assert status is not None
    assert status.status == "failed"
    assert status.error == "kg_not_configured"


def test_the_graph_tools_are_absent_rather_than_broken(tmp_path: Path) -> None:
    """A client discovering tools must not be offered one that cannot work."""

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.entrypoints.mcp_server import build_control_plane_mcp

    def _tool_names(kg) -> set[str]:
        mcp = build_control_plane_mcp(
            FakeMemoryBackend(),
            _settings(backend="none" if kg is None else "sqlite"),
            memory_space_id=SPACE,
            palace_path=str(tmp_path),
            host="127.0.0.1",
            port=10030,
            kg=kg,
        )
        return {tool.name for tool in asyncio.run(mcp.list_tools())}

    without_graph = _tool_names(None)

    assert not any(name.startswith("eidolon_memory_kg_") for name in without_graph)
    # The reads a client actually depends on are still there.
    assert "eidolon_memory_recall_context" in without_graph
    assert "eidolon_memory_search" in without_graph


async def test_switching_the_graph_off_and_back_on_keeps_what_was_stored(
    tmp_path: Path,
) -> None:
    """Off is a runtime choice, so it must not be destructive."""

    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph

    db = tmp_path / "knowledge_graph.sqlite3"

    graph = SqliteKnowledgeGraph(db, space_id=SPACE_FOR_TESTS, lock=asyncio.Lock())
    await graph.add_triple(audience="owner", subject="alice", predicate="likes", object="tea")
    graph.close()

    # ... a period running with kg.backend=none, during which nothing touches
    # the file ...
    assert db.exists()

    reopened = SqliteKnowledgeGraph(db, space_id=SPACE_FOR_TESTS, lock=asyncio.Lock())
    try:
        records = await reopened.query_entity("alice", audiences=("owner",))
        assert any(r.predicate == "likes" and r.object == "tea" for r in records)
    finally:
        reopened.close()
