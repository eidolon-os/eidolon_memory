"""Phase 5.2 — user-confirmed facts.

Three layers of contract to verify:
  1. Schema: explicit writes use the canonical ``MemoryIntentCommand``.
  2. Dispatch: ``process_command_message`` routes ``kind == "memory_intent"``
     to the explicit intent applier, which writes a verbatim drawer with
     ``metadata.source == "user-confirmed"``.
  3. Recall: ``recall_with_kg_fusion`` pins ``source == "user-confirmed"``
     records ahead of regular vector hits inside the same wing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_memory_contracts import (
    MemoryActorContext,
    MemoryIntent,
    MemoryIntentCommand,
    envelope_memory_payload,
)

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.explicit_intents import apply_explicit_intent
from eidolon.memory.application.public_recall import recall_with_kg_fusion
from eidolon.memory.application.turn_processor import process_command_message
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.canonical_fact import canonical_assertion_id
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger

MEMORY_SPACE_ID = "r:alice:default"


def _actor_context() -> MemoryActorContext:
    return MemoryActorContext(
        owner_id="alice",
        companion_id="companion-default",
        memory_realm_id=MEMORY_SPACE_ID,
        device_id="device",
        session_id="s1",
    )


def _command_wire(payload: dict) -> bytes:
    envelope = envelope_memory_payload(payload, kind=payload["kind"])
    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")


# ─── Schema ────────────────────────────────────────────────────────────────


def _intent_command(
    *,
    request_id: str = "r1",
    text: str = "我喝乌龙茶不喝咖啡",
    confidence: float = 0.99,
    attributes: dict | None = None,
) -> MemoryIntentCommand:
    return MemoryIntentCommand(
        request_id=request_id,
        memory_space_id=MEMORY_SPACE_ID,
        issued_at="2026-05-26T00:00:00Z",
        issuer="agent",
        intent=MemoryIntent(
            intent_id=f"intent:{request_id}",
            memory_space_id=MEMORY_SPACE_ID,
            source_event_id="turn-1",
            authority="explicit_user",
            intent_type="preference",
            raw_claim=text,
            operation_hint="confirm",
            confidence=confidence,
            attributes=attributes
            or {
                "wing": "Wing_Profile",
                "memory_type": "preference",
                "importance": 5,
                "tags": ["beverage"],
            },
        ),
    )


def _structured_command(
    request_id: str,
    *,
    source_event_id: str,
) -> MemoryIntentCommand:
    command = _intent_command(request_id=request_id)
    return command.model_copy(
        update={
            "intent": command.intent.model_copy(
                update={
                    "source_event_id": source_event_id,
                    "subject": "user",
                    "predicate": "likes",
                    "object": "oolong",
                }
            )
        }
    )


def _correction_command(
    request_id: str = "correct-1",
    *,
    source_event_id: str = "turn-2",
) -> MemoryIntentCommand:
    return MemoryIntentCommand(
        request_id=request_id,
        memory_space_id=MEMORY_SPACE_ID,
        issued_at="2026-05-27T00:00:00Z",
        issuer="agent",
        intent=MemoryIntent(
            intent_id=f"intent:{request_id}",
            memory_space_id=MEMORY_SPACE_ID,
            source_event_id=source_event_id,
            authority="explicit_user",
            intent_type="correction",
            raw_claim="我不再喜欢乌龙茶",
            operation_hint="invalidate",
            subject="user",
            predicate="likes",
            object="oolong",
        ),
    )


def test_cmd_defaults():
    cmd = _intent_command(attributes={})
    assert cmd.kind == "memory_intent"
    assert cmd.intent.operation_hint == "confirm"
    assert cmd.intent.authority == "explicit_user"
    assert cmd.intent.confidence == 0.99


def test_cmd_rejects_empty_text():
    """Pydantic ``min_length=1`` catches empty / whitespace-only callers."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        _intent_command(text=" ")


def test_cmd_validates_importance_and_confidence_bounds():
    import pydantic
    for confidence in (-0.1, 1.5):
        with pytest.raises(pydantic.ValidationError):
            _intent_command(confidence=confidence)


def test_cmd_rejects_cross_realm_intent():
    import pydantic

    raw = _intent_command().model_dump(mode="json")
    raw["memory_space_id"] = "r:bob:default"
    with pytest.raises(pydantic.ValidationError):
        MemoryIntentCommand.model_validate(raw)


# ─── explicit intent projection ───────────────────────────────────────────


async def test_ingest_writes_verbatim_drawer_with_source_marker():
    """The drawer the user wrote MUST land verbatim, NOT paraphrased."""
    backend = LockedBackend(FakeMemoryBackend())
    cmd = _intent_command(request_id="abc123")
    await apply_explicit_intent(backend, None, cmd)

    docs = list(backend._inner.docs.values())
    assert len(docs) == 1
    rec = docs[0]
    assert rec.value == "我喝乌龙茶不喝咖啡", "verbatim text was altered"
    assert rec.metadata.get("wing") == "Wing_Profile"
    assert rec.metadata.get("memory_type") == "preference"
    assert rec.metadata.get("source_turn_id") == "turn-1"

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
    """Same intent_id → same fragment_id → chroma dedups."""
    backend = LockedBackend(FakeMemoryBackend())
    cmd = _intent_command(request_id="dedup-key", text="x")
    for _ in range(4):
        await apply_explicit_intent(backend, None, cmd)
    assert len(backend._inner.docs) == 1


async def test_structured_intent_projects_drawer_and_kg_with_same_source_event():
    backend = LockedBackend(FakeMemoryBackend())
    kg = SimpleNamespace(add_triple=AsyncMock(return_value="triple-1"))
    base = _intent_command(request_id="structured")
    cmd = base.model_copy(
        update={
            "intent": base.intent.model_copy(
                update={
                    "intent_type": "preference",
                    "subject": "user",
                    "predicate": "likes",
                    "object": "oolong",
                }
            )
        }
    )

    resource_id = await apply_explicit_intent(backend, kg, cmd)

    assert resource_id == "memoryintent:intent:structured"
    assert len(backend._inner.docs) == 1
    kg.add_triple.assert_awaited_once_with(
        audience="owner",
        subject="user",
        predicate="likes",
        object="oolong",
        valid_from="2026-05-26T00:00:00Z",
        valid_to=None,
        confidence=0.99,
        source_turn_id="turn-1",
        adapter_name="user-confirmed",
    )


async def test_canonical_exact_fact_deduplicates_projection_but_keeps_evidence(
    tmp_path,
):
    backend = LockedBackend(FakeMemoryBackend())
    kg = SimpleNamespace(
        add_triple=AsyncMock(return_value="triple-1"),
        query_entity=AsyncMock(
            return_value=[
                SimpleNamespace(
                    subject="user",
                    predicate="likes",
                    object="oolong",
                )
            ]
        ),
    )
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    first = _intent_command(request_id="confirm-1")
    first = first.model_copy(
        update={
            "intent": first.intent.model_copy(
                update={
                    "subject": "user",
                    "predicate": "likes",
                    "object": "oolong",
                }
            )
        }
    )
    second = _intent_command(request_id="confirm-2")
    second = second.model_copy(
        update={
            "intent": second.intent.model_copy(
                update={
                    "source_event_id": "turn-2",
                    "subject": "user",
                    "predicate": "likes",
                    "object": "oolong",
                }
            )
        }
    )

    first_resource = await apply_explicit_intent(
        backend, kg, first, canonical_facts=canonical
    )
    second_resource = await apply_explicit_intent(
        backend, kg, second, canonical_facts=canonical
    )

    assert first_resource.startswith("memoryintent:fact:")
    assert second_resource.startswith("confirmed:fact:")
    assert second_resource.endswith(":evidence:2")
    assert len(backend.inner.docs) == 1
    assert kg.add_triple.await_count == 1


async def test_missing_canonical_drawer_is_reprojected_on_new_confirmation(
    tmp_path,
):
    backend = LockedBackend(FakeMemoryBackend())
    kg = SimpleNamespace(
        add_triple=AsyncMock(return_value="triple-1"),
        query_entity=AsyncMock(
            return_value=[
                SimpleNamespace(
                    subject="user",
                    predicate="likes",
                    object="oolong",
                )
            ]
        ),
    )
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    first = _structured_command("repair-drawer-1", source_event_id="turn-1")
    second = _structured_command("repair-drawer-2", source_event_id="turn-2")
    await apply_explicit_intent(backend, kg, first, canonical_facts=canonical)
    backend.inner.docs.clear()

    repaired = await apply_explicit_intent(
        backend, kg, second, canonical_facts=canonical
    )

    assert repaired.startswith("memoryintent:fact:")
    assert len(backend.inner.docs) == 1
    assert kg.add_triple.await_count == 1
    kg.query_entity.assert_awaited_once()


async def test_missing_canonical_kg_is_reprojected_on_new_confirmation(tmp_path):
    backend = LockedBackend(FakeMemoryBackend())
    kg = SimpleNamespace(
        add_triple=AsyncMock(return_value="triple-1"),
        query_entity=AsyncMock(return_value=[]),
    )
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    first = _structured_command("repair-kg-1", source_event_id="turn-1")
    second = _structured_command("repair-kg-2", source_event_id="turn-2")
    await apply_explicit_intent(backend, kg, first, canonical_facts=canonical)

    repaired = await apply_explicit_intent(
        backend, kg, second, canonical_facts=canonical
    )

    assert repaired.startswith("memoryintent:fact:")
    assert len(backend.inner.docs) == 1
    assert kg.add_triple.await_count == 2
    kg.query_entity.assert_awaited_once()


async def test_canonical_projection_remains_pending_until_all_projections_succeed(
    tmp_path,
):
    backend = LockedBackend(FakeMemoryBackend())
    kg = SimpleNamespace(
        add_triple=AsyncMock(
            side_effect=[RuntimeError("temporary KG failure"), "triple-1"]
        ),
        query_entity=AsyncMock(return_value=[]),
    )
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    command = _intent_command(request_id="projection-retry")
    command = command.model_copy(
        update={
            "intent": command.intent.model_copy(
                update={
                    "subject": "user",
                    "predicate": "likes",
                    "object": "oolong",
                }
            )
        }
    )

    with pytest.raises(RuntimeError, match="temporary KG failure"):
        await apply_explicit_intent(
            backend, kg, command, canonical_facts=canonical
        )
    resource_id = await apply_explicit_intent(
        backend, kg, command, canonical_facts=canonical
    )

    assert resource_id.startswith("memoryintent:fact:")
    assert len(backend.inner.docs) == 1
    assert kg.add_triple.await_count == 2


async def test_exact_correction_uses_canonical_lifecycle_and_is_replay_safe(
    tmp_path,
):
    backend = LockedBackend(FakeMemoryBackend())
    rows = {("user", "likes", "oolong")}

    async def _invalidate(**kwargs):
        key = (kwargs["subject"], kwargs["predicate"], kwargs["object"])
        if key not in rows:
            return 0
        rows.remove(key)
        return 1

    kg = SimpleNamespace(
        add_triple=AsyncMock(return_value="triple-1"),
        query_entity=AsyncMock(
            return_value=[
                SimpleNamespace(subject="user", predicate="likes", object="oolong")
            ]
        ),
        invalidate=AsyncMock(side_effect=_invalidate),
        find_invalidation_applied=AsyncMock(return_value=True),
    )
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    original = _structured_command("original", source_event_id="turn-1")
    await apply_explicit_intent(
        backend, kg, original, canonical_facts=canonical
    )
    correction = _correction_command()

    first = await apply_explicit_intent(
        backend, kg, correction, canonical_facts=canonical
    )
    replay = await apply_explicit_intent(
        backend, kg, correction, canonical_facts=canonical
    )

    assert first == replay
    drawer = await backend.get_by_source_turn_id(
        MEMORY_SPACE_ID,
        "canonical:"
        + canonical_assertion_id(MEMORY_SPACE_ID, "user", "likes", "oolong"),
    )
    assert drawer is not None
    assert drawer.metadata["privacy"] == "do_not_recall"
    # An already-applied lifecycle event is a full no-op. Replaying it after a
    # future reactivation must never end the new KG validity period.
    assert kg.invalidate.await_count == 1
    stats = await canonical.stats()
    assert stats.assertions_invalidated == 1
    assert stats.invalidations_total == 1


async def test_exact_correction_failure_stays_pending_and_retry_completes(
    tmp_path,
):
    backend = LockedBackend(FakeMemoryBackend())
    kg = SimpleNamespace(
        add_triple=AsyncMock(return_value="triple-1"),
        query_entity=AsyncMock(
            return_value=[
                SimpleNamespace(subject="user", predicate="likes", object="oolong")
            ]
        ),
        invalidate=AsyncMock(
            side_effect=[RuntimeError("temporary KG failure"), 1]
        ),
        find_invalidation_applied=AsyncMock(return_value=False),
    )
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    await apply_explicit_intent(
        backend,
        kg,
        _structured_command("original", source_event_id="turn-1"),
        canonical_facts=canonical,
    )
    correction = _correction_command("retry-correction")

    with pytest.raises(RuntimeError, match="temporary KG failure"):
        await apply_explicit_intent(
            backend, kg, correction, canonical_facts=canonical
        )
    pending = await canonical.stats()
    assert pending.assertions_active == 1
    assert pending.invalidations_pending == 1
    assert pending.invalidations_total == 0

    await apply_explicit_intent(
        backend, kg, correction, canonical_facts=canonical
    )
    completed = await canonical.stats()
    assert completed.assertions_active == 0
    assert completed.assertions_invalidated == 1
    assert completed.invalidations_pending == 0
    assert completed.invalidations_total == 1


async def test_correction_without_exact_triple_fails_closed(tmp_path):
    backend = LockedBackend(FakeMemoryBackend())
    canonical = CanonicalFactLedger(tmp_path / "canonical_facts.sqlite3")
    command = _intent_command(request_id="fuzzy-correction").model_copy(
        update={
            "intent": _intent_command().intent.model_copy(
                update={
                    "intent_type": "correction",
                    "operation_hint": "invalidate",
                    "raw_claim": "我不喜欢之前那个饮料了",
                }
            )
        }
    )

    with pytest.raises(ValueError, match="exact subject/predicate/object"):
        await apply_explicit_intent(
            backend,
            SimpleNamespace(),
            command,
            canonical_facts=canonical,
        )


# ─── Cmd dispatcher routes the kind ───────────────────────────────────────


async def test_process_command_message_dispatches_user_confirm():
    """The wire-level cmd payload reaches the explicit intent applier
    through ``process_command_message``'s elif branch."""
    backend = LockedBackend(FakeMemoryBackend())
    payload = _intent_command(
        request_id="wire-1", text="wire-shaped confirm"
    ).model_dump(mode="json")
    msg = SimpleNamespace(
        data=_command_wire(payload),
        ack=AsyncMock(),
    )
    settings = load_memory_settings()
    await process_command_message(
        msg, backend=backend, kg=None, settings=settings,
        expected_memory_space_id=MEMORY_SPACE_ID,
    )
    docs = list(backend._inner.docs.values())
    assert len(docs) == 1
    assert docs[0].value == "wire-shaped confirm"
    msg.ack.assert_awaited()


async def test_process_command_message_memory_space_mismatch_ignored():
    """Cross-space replay is dropped at the cmd
    dispatcher (existing guard); user-confirm inherits the same protection."""
    backend = LockedBackend(FakeMemoryBackend())
    command = _intent_command(request_id="wire-2", text="should not land")
    payload = command.model_dump(mode="json")
    payload["memory_space_id"] = "r:bob:default"
    payload["intent"]["memory_space_id"] = "r:bob:default"
    msg = SimpleNamespace(
        data=_command_wire(payload),
        ack=AsyncMock(),
    )
    settings = load_memory_settings()
    await process_command_message(
        msg, backend=backend, kg=None, settings=settings,
        expected_memory_space_id=MEMORY_SPACE_ID,
    )
    assert backend._inner.docs == {}
    msg.ack.assert_awaited()  # acked anyway — bad routing is not a NAK


async def test_update_intent_fails_closed_without_retry_or_projection():
    backend = LockedBackend(FakeMemoryBackend())
    base = _intent_command(request_id="unsafe-update")
    command = base.model_copy(
        update={
            "intent": base.intent.model_copy(update={"operation_hint": "update"})
        }
    )
    msg = SimpleNamespace(
        data=_command_wire(command.model_dump(mode="json")),
        ack=AsyncMock(),
        nak=AsyncMock(),
    )

    await process_command_message(
        msg,
        backend=backend,
        kg=None,
        settings=load_memory_settings(),
        expected_memory_space_id=MEMORY_SPACE_ID,
    )

    assert backend._inner.docs == {}
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()


# ─── Recall ranking pin ────────────────────────────────────────────────────


def _rec(value: str, *, source: str | None = None) -> MemoryWireRecord:
    meta = {"memory_type": "preference", "wing": "Wing_Profile"}
    if source:
        meta["source"] = source
    return MemoryWireRecord(
        memory_space_id=MEMORY_SPACE_ID, key=f"k-{abs(hash((value, source))) % 10_000}",
        value=value, metadata=meta,
    )


async def test_recall_pins_user_confirmed_ahead_of_regular():
    """User-confirmed drawer must surface ahead of cosine-ranked siblings."""
    # FakeMemoryBackend with three drawers — two regular + one user-confirmed.
    backend = LockedBackend(FakeMemoryBackend())
    settings = load_memory_settings()
    wing = next(w.id for w in settings.wings if w.id != "Wing_Privacy")

    async def _seed(key: str, text: str, *, source: str | None = None) -> None:
        meta: dict[str, object] = {
            "memory_space_id": MEMORY_SPACE_ID,
            "memory_type": "preference",
        }
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
        context=_actor_context(), top_k=5,
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
            metadata={"memory_space_id": MEMORY_SPACE_ID, "memory_type": "preference"},
        )
    result = await recall_with_kg_fusion(
        backend, settings,
        query="用户", context=_actor_context(), top_k=5, kg=None, for_voice=False,
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
        metadata={"memory_space_id": MEMORY_SPACE_ID, "memory_type": "preference"},
    )
    await backend.ingest_text(
        wing=wing, room="conf-A", text="用户喝乌龙茶",
        metadata={
            "memory_space_id": MEMORY_SPACE_ID,
            "memory_type": "preference",
            "source": "user-confirmed",
        },
    )
    await backend.ingest_text(
        wing=wing, room="conf-B", text="用户吃素",
        metadata={
            "memory_space_id": MEMORY_SPACE_ID,
            "memory_type": "preference",
            "source": "user-confirmed",
        },
    )

    result = await recall_with_kg_fusion(
        backend, settings,
        query="用户", context=_actor_context(), top_k=5, kg=None, for_voice=False,
    )
    sources = [(r.metadata or {}).get("source") for r in result["vector"]]
    # Two user-confirmed first (any order), then the regular one.
    assert sources[:2] == ["user-confirmed", "user-confirmed"], sources
    assert sources[-1] != "user-confirmed"
