"""Tests for LiveKit recall fail-fast and session filters."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from eidolon.memory.application.livekit_recall import LiveKitRecallService
from eidolon.memory.application.recall_filters import filter_voice_recall_hits
from eidolon.memory.config.memory_settings import MemorySettings, RecallPolicy, WingDefinition
from eidolon.memory.domain.errors import MemoryBackendUnavailable
from eidolon.memory.domain.wire import MemoryWireRecord


def _settings() -> MemorySettings:
    return MemorySettings(
        wings=[
            WingDefinition(id="Wing_Profile", display_name="p"),
            WingDefinition(id="Wing_Privacy", display_name="x"),
        ],
        recall=RecallPolicy(
            livekit_timeout_seconds=0.2,
            voice_wings=["Wing_Profile"],
            exclude_current_session=True,
            exclude_recent_minutes=0,
        ),
    )


@pytest.mark.asyncio
async def test_livekit_recall_fail_fast_on_error() -> None:
    session = MagicMock()
    session.ensure_fresh = AsyncMock()
    session.background_reconcile = AsyncMock()
    backend = MagicMock()
    session.active_backend = AsyncMock(return_value=backend)

    async def _boom(*_a, **_k):
        raise MemoryBackendUnavailable("Error finding id")

    import eidolon.memory.application.livekit_recall as mod

    original = mod.search_all_wings_mcp_style
    mod.search_all_wings_mcp_style = _boom
    try:
        svc = LiveKitRecallService(session, _settings())
        out = await svc.recall_context_with_records("hello", session_id="s1")
        assert out["context"] == ""
        assert out["degraded"] is True
    finally:
        mod.search_all_wings_mcp_style = original


def test_filter_excludes_same_session() -> None:
    settings = _settings()
    hits = [
        MemoryWireRecord(
            user_id="Wing_Profile",
            key="k1",
            value="same",
            metadata={"session_id": "sess-a"},
        ),
        MemoryWireRecord(
            user_id="Wing_Profile",
            key="k2",
            value="old",
            metadata={"session_id": "other"},
        ),
    ]
    out = filter_voice_recall_hits(hits, settings, session_id="sess-a")
    assert len(out) == 1
    assert out[0].value == "old"
