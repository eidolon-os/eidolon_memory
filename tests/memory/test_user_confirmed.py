"""Phase 5.2 — user-confirmed facts.

Three layers of contract to verify:
  1. Schema: ``UserConfirmedFactCommand`` validates + has correct defaults.
  2. Dispatch: ``process_command_message`` routes ``kind == "user_confirm_fact"``
     to ``_ingest_user_confirmed``, which writes a verbatim drawer with
     ``metadata.source == "user-confirmed"``.
  3. Recall: ``recall_with_kg_fusion`` pins ``source == "user-confirmed"``
     records ahead of regular vector hits inside the same wing.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.public_recall import recall_with_kg_fusion
from eidolon.memory.application.turn_processor import (
    _ingest_user_confirmed,
    process_command_message,
)
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon_sdk.memory import UserConfirmedFactCommand
from eidolon.memory.domain.wire import MemoryWireRecord

pytestmark = pytest.mark.asyncio


# ─── Schema ────────────────────────────────────────────────────────────────


def test_cmd_defaults():
    cmd = UserConfirmedFactCommand(
        request_id="r1", user_id="alice",
        issued_at="2026-05-26T00:00:00Z", issuer="agent",
        text="我喝乌龙茶不喝咖啡", wing="Wing_Profile",
    )
    assert cmd.kind == "user_confirm_fact"
    assert cmd.memory_type == "profile"
    assert cmd.importance == 5
    assert cmd.confidence == 0.99
    assert cmd.tags == []


def test_cmd_rejects_empty_text():
    """Pydantic ``min_length=1`` catches empty / whitespace-only callers."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        UserConfirmedFactCommand(
            request_id="r1", user_id="alice",
            issued_at="2026-05-26T00:00:00Z",
            text="", wing="Wing_Profile",
        )


def test_cmd_validates_importance_and_confidence_bounds():
    import pydantic
    base = dict(
        request_id="r1", user_id="alice",
        issued_at="2026-05-26T00:00:00Z",
        text="x", wing="Wing_Profile",
    )
    for bad in ({"importance": 0}, {"importance": 6},
                {"confidence": -0.1}, {"confidence": 1.5}):
        with pytest.raises(pydantic.ValidationError):
            UserConfirmedFactCommand(**base, **bad)


def test_cmd_replay_safe_without_optional_fields():
    """Older callers may not send ``tags`` / ``memory_type`` — defaults kick in."""
    raw = {
        "kind": "user_confirm_fact",
        "request_id": "r1", "user_id": "alice",
        "issued_at": "2026-05-26T00:00:00Z", "issuer": "agent",
        "text": "verbatim", "wing": "Wing_Profile",
    }
    cmd = UserConfirmedFactCommand.model_validate(raw)
    assert cmd.tags == []
    assert cmd.memory_type == "profile"


# ─── _ingest_user_confirmed ────────────────────────────────────────────────


async def test_ingest_writes_verbatim_drawer_with_source_marker():
    """The drawer the user wrote MUST land verbatim, NOT paraphrased."""
    backend = LockedBackend(FakeMemoryBackend())
    cmd = UserConfirmedFactCommand(
        request_id="abc123", user_id="alice",
        issued_at="2026-05-26T00:00:00Z", issuer="agent",
        text="我喝乌龙茶不喝咖啡", wing="Wing_Profile",
        memory_type="preference",
        importance=5,
        confidence=0.99,
        tags=["beverage"],
    )
    await _ingest_user_confirmed(backend, cmd)

    docs = list(backend._inner.docs.values())
    assert len(docs) == 1
    rec = docs[0]
    assert rec.value == "我喝乌龙茶不喝咖啡", "verbatim text was altered"
    assert rec.metadata.get("wing") == "Wing_Profile"
    assert rec.metadata.get("memory_type") == "preference"

    # Contract: the metadata PASSED in through ingest carries the source
    # marker that recall keys off. FakeBackend overrides ``source`` with
    # its own "fake" tag at the adapter boundary; assert against ingests
    # log instead — that's what real chroma would persist.
    _, _, _, passed_meta = backend._inner.ingests[-1]
    assert passed_meta.get("source") == "user-confirmed", (
        f"missing source marker in ingest metadata: {passed_meta}"
    )
    assert "user-confirmed" in passed_meta.get("tags", [])


async def test_ingest_idempotent_on_redelivery():
    """Same request_id → same fragment_id → chroma dedups."""
    backend = LockedBackend(FakeMemoryBackend())
    cmd = UserConfirmedFactCommand(
        request_id="dedup-key", user_id="alice",
        issued_at="2026-05-26T00:00:00Z",
        text="x", wing="Wing_Profile",
    )
    for _ in range(4):
        await _ingest_user_confirmed(backend, cmd)
    assert len(backend._inner.docs) == 1


# ─── Cmd dispatcher routes the kind ───────────────────────────────────────


async def test_process_command_message_dispatches_user_confirm():
    """The wire-level cmd payload reaches ``_ingest_user_confirmed``
    through ``process_command_message``'s elif branch."""
    backend = LockedBackend(FakeMemoryBackend())
    payload = {
        "kind": "user_confirm_fact",
        "request_id": "wire-1", "user_id": "alice",
        "issued_at": "2026-05-26T00:00:00Z", "issuer": "agent",
        "text": "wire-shaped confirm", "wing": "Wing_Profile",
    }
    msg = SimpleNamespace(
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ack=AsyncMock(),
    )
    settings = load_memory_settings()
    await process_command_message(
        msg, backend=backend, kg=None, settings=settings, expected_user_id="alice",
    )
    docs = list(backend._inner.docs.values())
    assert len(docs) == 1
    assert docs[0].value == "wire-shaped confirm"
    msg.ack.assert_awaited()


async def test_process_command_message_user_id_mismatch_ignored():
    """Cross-user replay (cmd from another user_id) is dropped at the cmd
    dispatcher (existing guard); user-confirm inherits the same protection."""
    backend = LockedBackend(FakeMemoryBackend())
    payload = {
        "kind": "user_confirm_fact",
        "request_id": "wire-2", "user_id": "bob",   # mismatch
        "issued_at": "2026-05-26T00:00:00Z", "issuer": "agent",
        "text": "should not land", "wing": "Wing_Profile",
    }
    msg = SimpleNamespace(
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ack=AsyncMock(),
    )
    settings = load_memory_settings()
    await process_command_message(
        msg, backend=backend, kg=None, settings=settings, expected_user_id="alice",
    )
    assert backend._inner.docs == {}
    msg.ack.assert_awaited()  # acked anyway — bad routing is not a NAK


# ─── Recall ranking pin ────────────────────────────────────────────────────


def _rec(value: str, *, source: str | None = None) -> MemoryWireRecord:
    meta = {"memory_type": "preference", "wing": "Wing_Profile"}
    if source:
        meta["source"] = source
    return MemoryWireRecord(
        user_id="alice", key=f"k-{abs(hash((value, source))) % 10_000}",
        value=value, metadata=meta,
    )


async def test_recall_pins_user_confirmed_ahead_of_regular():
    """User-confirmed drawer must surface ahead of cosine-ranked siblings."""
    # FakeMemoryBackend with three drawers — two regular + one user-confirmed.
    backend = LockedBackend(FakeMemoryBackend())
    settings = load_memory_settings()
    wing = next(w.id for w in settings.wings if w.id != "Wing_Privacy")

    async def _seed(key: str, text: str, *, source: str | None = None) -> None:
        meta: dict[str, object] = {"memory_type": "preference"}
        if source:
            meta["source"] = source
        await backend.ingest_text(
            wing=wing, room=key, text=text, metadata=meta,
        )

    await _seed("noise-1", "用户喜欢看书")
    await _seed("noise-2", "用户喜欢散步")
    await _seed("confirm-1", "用户喝乌龙茶不喝咖啡", source="user-confirmed")

    result = await recall_with_kg_fusion(
        backend, settings,
        query="用户",        # FakeBackend substring filter → all three returned
        user_id=wing, top_k=5,
        kg=None, for_voice=False,
    )
    values = [r.value for r in result["vector"]]
    assert values[0] == "用户喝乌龙茶不喝咖啡", (
        f"user-confirmed not pinned to top; got {values}"
    )


async def test_recall_unchanged_when_no_user_confirmed_present():
    """No source=user-confirmed records → behaviour identical to pre-5.2."""
    backend = LockedBackend(FakeMemoryBackend())
    settings = load_memory_settings()
    wing = next(w.id for w in settings.wings if w.id != "Wing_Privacy")
    for i, text in enumerate(["alpha", "beta", "gamma"]):
        await backend.ingest_text(
            wing=wing, room=f"k{i}", text=f"用户 {text}",
            metadata={"memory_type": "preference"},
        )
    result = await recall_with_kg_fusion(
        backend, settings,
        query="用户", user_id=wing, top_k=5, kg=None, for_voice=False,
    )
    # No user-confirmed → no reordering; only assertion is non-empty + no errors.
    assert len(result["vector"]) == 3
    for r in result["vector"]:
        assert (r.metadata or {}).get("source") != "user-confirmed"


async def test_recall_pins_multiple_user_confirmed_then_others():
    """Multiple user-confirmed drawers all pin ahead, preserving inner order."""
    backend = LockedBackend(FakeMemoryBackend())
    settings = load_memory_settings()
    wing = next(w.id for w in settings.wings if w.id != "Wing_Privacy")

    # Two user-confirmed + one regular.
    await backend.ingest_text(
        wing=wing, room="reg-1", text="用户散步",
        metadata={"memory_type": "preference"},
    )
    await backend.ingest_text(
        wing=wing, room="conf-A", text="用户喝乌龙茶",
        metadata={"memory_type": "preference", "source": "user-confirmed"},
    )
    await backend.ingest_text(
        wing=wing, room="conf-B", text="用户吃素",
        metadata={"memory_type": "preference", "source": "user-confirmed"},
    )

    result = await recall_with_kg_fusion(
        backend, settings,
        query="用户", user_id=wing, top_k=5, kg=None, for_voice=False,
    )
    sources = [(r.metadata or {}).get("source") for r in result["vector"]]
    # Two user-confirmed first (any order), then the regular one.
    assert sources[:2] == ["user-confirmed", "user-confirmed"], sources
    assert sources[-1] != "user-confirmed"
