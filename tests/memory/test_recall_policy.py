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


def test_user_confirmed_authority_outranks_same_session_recency() -> None:
    context = MemoryActorContext(
        memory_realm_id="default.alice.default",
        memory_space_id="default.alice.default",
        session_id="current-session",
    )
    current_chat = MemoryWireRecord(
        memory_space_id=context.memory_space_id,
        key="drawer_chat",
        value="当前会话普通内容",
        metadata={
            "memory_space_id": context.memory_space_id,
            "session_id": "current-session",
            "scope": "persona",
            "similarity": 0.99,
        },
    )
    explicit = MemoryWireRecord(
        memory_space_id=context.memory_space_id,
        key="drawer_explicit",
        value="用户明确要求记住的内容",
        metadata={
            "memory_space_id": context.memory_space_id,
            "source": "user-confirmed",
            "scope": "global",
            "similarity": 0.8,
        },
    )

    ranked = RecallPolicyRegistry.default().rank(
        [current_chat, explicit],
        context=context,
        query="内容",
        top_k=2,
    )

    assert [record.key for record in ranked] == ["drawer_explicit", "drawer_chat"]
