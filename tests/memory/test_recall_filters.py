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


def test_fresh_natural_turn_is_suppressed_but_user_confirmed_is_visible() -> None:
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
    confirmed = MemoryWireRecord(
        memory_space_id="realm-1",
        key="userconfirm:fact-1",
        value="owner likes 火龙果",
        metadata={
            "session_id": "session-1",
            "filed_at": now,
            "source": "user-confirmed",
            "room": "userconfirm:fact-1",
        },
    )

    out = filter_voice_recall_hits(
        [natural, confirmed],
        settings,
        session_id="session-1",
    )

    assert out == [confirmed]


def test_user_confirmed_exact_utterance_is_not_mistaken_for_chat_echo() -> None:
    settings = MemorySettings(
        recall=RecallPolicy(exclude_current_session=False, exclude_recent_minutes=0),
    )
    confirmed = MemoryWireRecord(
        memory_space_id="realm-1",
        key="userconfirm:fact-1",
        value="owner likes 火龙果",
        metadata={"source": "user-confirmed"},
    )

    assert filter_voice_recall_hits(
        [confirmed],
        settings,
        user_utterance="owner likes 火龙果",
    ) == [confirmed]
