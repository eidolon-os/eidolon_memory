"""What the service reports about itself, and what it costs to report it.

Two things have to hold. The numbers must answer the questions an operator
actually has — where recall spent its time, whether an empty answer was correct
or a failure. And recording them must be cheap enough for the voice path, which
has the least room of anything here.
"""

from __future__ import annotations

import asyncio

import pytest
from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.public_recall import recall_with_kg_fusion
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.support import metrics, tracing

SPACE = "default.alice.default"


def _settings(**kg) -> MemorySettings:
    return MemorySettings.model_validate({"kg": kg} if kg else {})


def _context() -> MemoryActorContext:
    return MemoryActorContext(memory_realm_id=SPACE, owner_id="alice")


def _samples(name: str) -> dict[tuple, float]:
    """Collect one metric's samples, keyed by label values."""

    from prometheus_client import REGISTRY

    collected: dict[tuple, float] = {}
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name.startswith(name):
                collected[tuple(sorted(sample.labels.items()))] = sample.value
    return collected


def _count(name: str, **labels: str) -> float:
    wanted = tuple(sorted(labels.items()))
    return _samples(name).get(wanted, 0.0)


# ── what the numbers say ─────────────────────────────────────────────────────


async def test_an_empty_recall_is_distinguished_from_a_failed_one() -> None:
    """They look identical to a caller and mean opposite things to an operator.

    Empty is a correct answer about a space with nothing relevant. Degraded is us
    failing to look properly. Collapsing them would hide outages behind what
    looks like a quiet user.
    """

    before = _count("eidolon_memory_recall_total", kind="chat", outcome="empty")

    await recall_with_kg_fusion(
        FakeMemoryBackend(),
        _settings(backend="none"),
        query="nothing stored yet",
        context=_context(),
        top_k=5,
        kg=None,
        for_voice=False,
        palace_path=None,
    )

    after = _count("eidolon_memory_recall_total", kind="chat", outcome="empty")
    assert after == before + 1


async def test_a_recall_that_found_something_is_counted_as_a_hit() -> None:
    backend = FakeMemoryBackend()
    await backend.ingest_text(
        wing="Wing_Life",
        room="colour",
        text="likes the colour green",
        metadata={"memory_space_id": SPACE},
    )
    before = _count("eidolon_memory_recall_total", kind="chat", outcome="hit")

    await recall_with_kg_fusion(
        backend,
        _settings(backend="none"),
        query="colour",
        context=_context(),
        top_k=5,
        kg=None,
        for_voice=False,
        palace_path=None,
    )

    assert _count("eidolon_memory_recall_total", kind="chat", outcome="hit") == before + 1


async def test_voice_and_chat_are_measured_separately() -> None:
    """One distribution over both would hide both: the budgets differ by 2x."""

    for voice in (True, False):
        await recall_with_kg_fusion(
            FakeMemoryBackend(),
            _settings(backend="none"),
            query="anything",
            context=_context(),
            top_k=5,
            kg=None,
            for_voice=voice,
            palace_path=None,
        )

    kinds = {
        dict(labels).get("kind")
        for labels in _samples("eidolon_memory_recall_seconds").keys()
    }
    assert {"voice", "chat"} <= kinds


async def test_latency_is_recorded_against_the_configured_backend() -> None:
    """Comparing a local file to a vector server needs the two kept apart."""

    await recall_with_kg_fusion(
        FakeMemoryBackend(),
        _settings(backend="none"),
        query="anything",
        context=_context(),
        top_k=5,
        kg=None,
        for_voice=False,
        palace_path=None,
    )

    backends = {
        dict(labels).get("backend")
        for labels in _samples("eidolon_memory_recall_seconds").keys()
    }
    assert "chroma" in backends


# ── spans ────────────────────────────────────────────────────────────────────


def test_a_span_carries_the_trace_it_belongs_to() -> None:
    with tracing.span("outer", trace_id="trace-1") as attributes:
        attributes["result_count"] = 3
        assert tracing.current_trace_id() == "trace-1"


def test_a_nested_span_inherits_the_trace_without_being_passed_it() -> None:
    """Otherwise every function in the path would need a trace parameter."""

    with tracing.span("outer", trace_id="trace-1"):
        with tracing.span("inner"):
            assert tracing.current_trace_id() == "trace-1"


def test_work_arriving_without_a_trace_gets_one() -> None:
    with tracing.span("orphan"):
        assert tracing.current_trace_id()


def test_a_span_never_swallows_a_failure() -> None:
    """A span that hid an exception would be worse than no span at all."""

    with pytest.raises(ValueError, match="boom"):
        with tracing.span("failing"):
            raise ValueError("boom")


def test_a_span_leaves_no_trace_behind_after_it_ends() -> None:
    """Otherwise a later, unrelated turn would log someone else's trace id."""

    with tracing.span("outer", trace_id="trace-1"):
        pass

    assert tracing.current_trace_id() is None


def test_a_failed_span_is_recorded_even_when_sampling_would_drop_it() -> None:
    """Sampling exists to keep the happy path cheap. Dropping failures is how an
    incident becomes invisible."""

    recorded: list[tuple[str, dict]] = []

    class _Capture:
        def info(self, message: str, **fields) -> None:
            recorded.append((message, fields))

    original = tracing.log
    tracing.log = _Capture()
    try:
        with pytest.raises(RuntimeError):
            with tracing.span("failing", sample=0.0):
                raise RuntimeError("nope")
    finally:
        tracing.log = original

    assert [name for name, _ in recorded] == ["failing"]
    assert recorded[0][1]["error"] == "RuntimeError"


def test_sampling_can_drop_a_successful_span(monkeypatch) -> None:
    recorded: list[str] = []

    class _Capture:
        def info(self, message: str, **fields) -> None:
            recorded.append(message)

    original = tracing.log
    tracing.log = _Capture()
    try:
        with tracing.span("quiet", sample=0.0):
            pass
    finally:
        tracing.log = original

    assert recorded == []


def test_the_voice_sample_rate_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("EIDOLON_MEMORY_TRACE_SAMPLE", "0.5")
    assert tracing.voice_sample_rate() == 0.5

    monkeypatch.setenv("EIDOLON_MEMORY_TRACE_SAMPLE", "nonsense")
    assert 0.0 < tracing.voice_sample_rate() <= 1.0

    monkeypatch.setenv("EIDOLON_MEMORY_TRACE_SAMPLE", "9")
    assert tracing.voice_sample_rate() == 1.0


# ── cost ─────────────────────────────────────────────────────────────────────


def test_recording_is_cheap_enough_for_the_voice_path() -> None:
    """The voice budget is ~100ms; instrumentation must not be a visible slice.

    Generous threshold on purpose — this catches a regression that makes
    recording expensive, not small variations between machines.
    """

    import time

    iterations = 2_000
    started = time.perf_counter()
    for _ in range(iterations):
        metrics.RECALL_SECONDS.labels(
            kind="voice", backend="chroma", graph="off", degraded="false"
        ).observe(0.01)
    per_observation_us = (time.perf_counter() - started) / iterations * 1_000_000

    assert per_observation_us < 50, f"{per_observation_us:.1f}us per observation"


def test_the_service_runs_with_prometheus_absent() -> None:
    """A client that only speaks the protocol has no reason to install it.

    Every helper works against a no-op, so nothing in the service has to branch
    on whether metrics are available.
    """

    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent(
        """
        import builtins, sys
        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.split(".")[0] == "prometheus_client":
                raise ModuleNotFoundError("No module named 'prometheus_client'")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = refuse
        for loaded in [m for m in sys.modules if m.startswith(("prometheus_client",
                                                              "eidolon.memory.support"))]:
            del sys.modules[loaded]

        from eidolon.memory.support import metrics
        assert not metrics.METRICS_AVAILABLE
        # Observing must still be safe, not merely importable.
        metrics.RECALL_SECONDS.labels(
            kind="voice", backend="chroma", graph="off", degraded="false"
        ).observe(0.01)
        metrics.RECALL_TOTAL.labels(kind="voice", outcome="hit").inc()
        metrics.SPACES_HELD.set(3)
        assert metrics.render_metrics() == b""
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_exposition_payload_is_scrapeable() -> None:
    metrics.RECALL_TOTAL.labels(kind="chat", outcome="hit").inc()

    payload = metrics.render_metrics()

    assert b"eidolon_memory_recall_total" in payload
    assert "text/plain" in metrics.metrics_content_type()


async def test_holding_more_spaces_is_visible(tmp_path, monkeypatch) -> None:
    """The number that says whether the embedding model is being amortised."""

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter

    settings = MemorySettings.model_validate(
        {
            "runtime": {
                "palaces_root": str(tmp_path / "palaces"),
                "run_dir": str(tmp_path / "run"),
            },
            "mempalace": {"backend": "chroma", "offline_embedding": True},
            "kg": {"backend": "none"},
        }
    )
    router = LocalPalaceRouter(settings)
    try:
        await router.resolve("alice")
        await router.resolve("bob")

        assert _count("eidolon_memory_spaces_held") == 2
    finally:
        await router.aclose()

    assert _count("eidolon_memory_spaces_held") == 0


def test_opening_a_space_is_timed(tmp_path, monkeypatch) -> None:
    """Startup cost per space is what bounds how many one process can serve."""

    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter

    settings = MemorySettings.model_validate(
        {
            "runtime": {
                "palaces_root": str(tmp_path / "palaces"),
                "run_dir": str(tmp_path / "run"),
            },
            "mempalace": {"backend": "chroma", "offline_embedding": True},
            "kg": {"backend": "none"},
        }
    )
    router = LocalPalaceRouter(settings)
    try:
        asyncio.run(router.resolve("alice"))
    finally:
        asyncio.run(router.aclose())

    assert _count("eidolon_memory_space_open_seconds_count") >= 1


# ── the write path ───────────────────────────────────────────────────────────


async def test_a_turn_reports_whether_it_stored_anything(tmp_path) -> None:
    """A steady stream of skipped turns is either a quiet conversation or a
    broken classifier, and only the ratio tells them apart."""

    import json
    from types import SimpleNamespace

    from eidolon_memory_contracts import (
        ConversationTurnPayload,
        envelope_memory_payload,
    )

    from eidolon.memory.application.turn_processor import process_turn_message

    class _SkippingSteward:
        extraction_version = "test-1"

        async def decide(self, turn):
            from eidolon.memory.domain.steward import StewardDecision

            return StewardDecision(should_write=False, reason="low signal")

    payload = ConversationTurnPayload(
        turn_id="turn-metrics",
        context=_context(),
        user_text="hello",
        assistant_text="hi",
        timestamp="2026-08-01T10:00:00Z",
    )
    envelope = envelope_memory_payload(payload, kind="conversation_turn")

    acks: list[str] = []

    async def _ack() -> None:
        acks.append("ack")

    msg = SimpleNamespace(
        data=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        subject="eidolon.memory.turn.test",
        ack=_ack,
        nak=_ack,
        metadata=SimpleNamespace(num_delivered=1),
    )

    before = _count("eidolon_memory_turn_total", outcome="skipped")

    await process_turn_message(
        msg,
        steward=_SkippingSteward(),
        backend=FakeMemoryBackend(),
        kg=None,
        settings=_settings(backend="none"),
        max_deliveries=3,
        expected_memory_space_id=SPACE,
    )

    assert acks == ["ack"]
    assert _count("eidolon_memory_turn_total", outcome="skipped") == before + 1
    # The steward's own time is separated out because it waits on an LLM and
    # therefore dominates everything else a turn does.
    assert _count("eidolon_memory_turn_stage_seconds_count", stage="steward") >= 1
