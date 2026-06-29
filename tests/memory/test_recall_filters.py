"""Session dedupe filters for voice recall."""

from __future__ import annotations

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
