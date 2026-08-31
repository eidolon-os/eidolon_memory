"""The read/write result contract, and what it deliberately does not carry."""

from datetime import datetime

import pytest

from eidolon_memory_contracts import (
    MemoryReadContract,
    MemorySnippet,
    MemoryWriteContract,
    RecallPlan,
    RecallResult,
    TurnPublishReceipt,
    WriteOutcome,
)


def test_recall_result_does_not_leak_service_internals() -> None:
    """A client must not be able to tell how memory is stored or fused.

    Both of these fields existed on the wire before and are what let a caller
    infer that the service keeps a knowledge graph and an in-process turn ring.
    """

    fields = set(RecallResult.model_fields)

    assert "kg_triples" not in fields
    assert "working_memory" not in fields


def test_recall_result_serialises_exactly_the_agreed_fields() -> None:
    """Pins the emitted shape, so re-adding an internal field is a test failure.

    Validation itself is lenient — an unknown key on the way in is ignored
    rather than rejected, which is what lets a JetStream replay of an older
    message still parse. Strictness belongs on what we emit.
    """

    result = RecallResult.model_validate({"context": "x", "snippets": []})

    assert result.model_dump().keys() == {
        "context",
        "snippets",
        "degraded",
        "degraded_reason",
    }


def test_degraded_recall_is_still_structurally_usable() -> None:
    """Callers render a degraded result without None-checking every field."""

    result = RecallResult(degraded=True, degraded_reason="timeout")

    assert result.context == ""
    assert result.snippets == []


def test_snippet_speaks_the_clients_vocabulary_not_the_stores() -> None:
    fields = set(MemorySnippet.model_fields)

    assert {"id", "text"} <= fields
    assert not {"key", "value", "drawer_id", "memory_space_id"} & fields


def test_snippet_parses_iso_timestamps_off_the_wire() -> None:
    snippet = MemorySnippet.model_validate(
        {"id": "m1", "text": "likes green", "memory_time": "2026-08-01T10:00:00Z"}
    )

    assert isinstance(snippet.memory_time, datetime)


def test_only_applied_counts_as_durable() -> None:
    for status in ("accepted", "retrying", "failed", "unknown"):
        assert not WriteOutcome(status=status, request_id="r1").durable

    assert WriteOutcome(status="applied", request_id="r1").durable


def test_write_status_outside_the_five_states_is_rejected() -> None:
    with pytest.raises(ValueError):
        WriteOutcome(status="ok", request_id="r1")


def test_publishing_state_is_not_absorption() -> None:
    """A receipt says the bus took it, so it carries no resource id."""

    fields = set(TurnPublishReceipt.model_fields)

    assert "state" in fields
    assert "resource_id" not in fields


def test_focus_subjects_defaults_to_no_hint() -> None:
    assert RecallPlan().focus_subjects == ()


def test_contracts_are_runtime_checkable_against_duck_typed_clients() -> None:
    """Implementors need not inherit; the protocols are structural."""

    class Reader:
        async def recall_context(self, ctx, query, *, plan, timeout_s=0.2): ...
        async def search(self, ctx, query, *, top_k=5, timeout_s=0.2): ...
        async def read_active_commitments(self, ctx, *, limit=5, timeout_s=0.2): ...
        async def get_by_source_turn(self, ctx, source_turn_id, *, timeout_s=0.5): ...
        async def preview_forget(self, ctx, query, *, action="archive", timeout_s=0.5): ...
        async def command_status(self, ctx, request_id, *, timeout_s=0.5): ...
        async def status(self, ctx): ...
        async def health(self): ...

    class Writer:
        async def publish_turn(self, turn, *, trace_id=None): ...
        async def confirm_forget(self, ctx, confirmation_token, *, wait_applied_seconds=2.0): ...

    assert isinstance(Reader(), MemoryReadContract)
    assert isinstance(Writer(), MemoryWriteContract)
    assert not isinstance(object(), MemoryReadContract)
