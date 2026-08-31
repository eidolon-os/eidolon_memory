"""Recall policy filtering (taboo / top_k)."""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.mempalace_python_backend import apply_recall_policy
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.wire import MemoryWireRecord


def test_taboo_filtered(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    hits = [
        MemoryWireRecord(
            memory_space_id="a", key="b", value="keep", metadata={"room_status": "active"}
        ),
        MemoryWireRecord(
            memory_space_id="a", key="c", value="gone", metadata={"room_status": "taboo"}
        ),
    ]
    out = apply_recall_policy(hits, settings)
    assert len(out) == 1
    assert out[0].value == "keep"


def test_top_k_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    hits = [
        MemoryWireRecord(memory_space_id="a", key=str(i), value=str(i), metadata={})
        for i in range(20)
    ]
    out = apply_recall_policy(hits, settings)
    assert len(out) <= settings.recall.top_k


def test_rank_does_not_use_transport_session_as_relevance() -> None:
    context = MemoryActorContext(
        memory_realm_id="default.alice.default",
        memory_space_id="default.alice.default",
        companion_id="default",
        session_id="current-session",
    )
    same_session = MemoryWireRecord(
        memory_space_id=context.memory_space_id,
        key="same_session",
        value="同一交互写入的事实",
        metadata={
            "memory_space_id": context.memory_space_id,
            "session_id": "current-session",
            "scope": "persona",
            "similarity": 0.7,
        },
    )
    older_session = MemoryWireRecord(
        memory_space_id=context.memory_space_id,
        key="older_session",
        value="较早交互写入但更相关的事实",
        metadata={
            "memory_space_id": context.memory_space_id,
            "session_id": "older-session",
            "scope": "persona",
            "similarity": 0.9,
        },
    )

    ranked = RecallPolicyRegistry.default().rank(
        [same_session, older_session],
        context=context,
        query="事实",
        top_k=2,
    )

    assert [record.key for record in ranked] == ["older_session", "same_session"]
