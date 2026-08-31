"""Session dedupe filters for voice recall."""

from __future__ import annotations

from datetime import UTC, datetime

from eidolon.memory.application.recall_filters import filter_voice_recall_hits
from eidolon.memory.config.memory_settings import MemorySettings, RecallPolicy
from eidolon.memory.domain.wire import MemoryWireRecord


def test_exclude_duplicate_utterance() -> None:
    settings = MemorySettings(
        recall=RecallPolicy(exclude_current_session=False, exclude_recent_minutes=0),
    )
    hits = [
        MemoryWireRecord(
            memory_space_id="Wing_Profile",
            key="k",
            value="Hello World",
            metadata={},
        )
    ]
    out = filter_voice_recall_hits(
        hits,
        settings,
        user_utterance="hello world",
    )
    assert out == []


def test_fresh_same_session_records_are_suppressed_without_source_exceptions() -> None:
    settings = MemorySettings(
        recall=RecallPolicy(exclude_current_session=True, exclude_recent_minutes=5),
    )
    now = datetime.now(UTC).isoformat()
    natural = MemoryWireRecord(
        memory_space_id="realm-1",
        key="profile_core",
        value="用户喜欢火龙果",
        metadata={
            "session_id": "session-1",
            "filed_at": now,
            "source": "steward-llm",
        },
    )
    command_projection = MemoryWireRecord(
        memory_space_id="realm-1",
        key="assertion:fact-1",
        value="owner likes 火龙果",
        metadata={
            "session_id": "session-1",
            "filed_at": now,
            "source": "assertion-ledger",
            "room": "assertion:fact-1",
        },
    )

    out = filter_voice_recall_hits(
        [natural, command_projection],
        settings,
        session_id="session-1",
    )

    assert out == []


def test_exact_utterance_is_suppressed_for_every_projection_source() -> None:
    settings = MemorySettings(
        recall=RecallPolicy(exclude_current_session=False, exclude_recent_minutes=0),
    )
    record = MemoryWireRecord(
        memory_space_id="realm-1",
        key="assertion:fact-1",
        value="owner likes 火龙果",
        metadata={"source": "assertion-ledger"},
    )

    assert (
        filter_voice_recall_hits(
            [record],
            settings,
            user_utterance="owner likes 火龙果",
        )
        == []
    )
