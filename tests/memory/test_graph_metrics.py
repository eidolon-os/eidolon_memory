"""What ``/metrics`` says about the graph, and when it says it.

``GRAPH_TIMEOUTS`` records that a recall dropped the graph and its own docstring
says a climbing rate is the signal — but nothing could say *why* it climbed. These
gauges are the answer, so the properties worth pinning are that they are published
at all, that they are published on a space nobody is writing to, and that a broken
graph does not take the runner down with it.
"""

from __future__ import annotations

import asyncio

import pytest

from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
from eidolon.memory.domain.space_lock import SpaceLock
from eidolon.memory.entrypoints.agent_runner import (
    GRAPH_SAMPLE_SECONDS,
    publish_graph_size,
)
from eidolon.memory.support import metrics

SPACE = "default.alice.default"

pytestmark = pytest.mark.skipif(
    not metrics.METRICS_AVAILABLE,
    reason="prometheus_client is optional; the no-op metrics record nothing to read",
)


def _value(gauge, **labels) -> float:
    target = gauge.labels(**labels) if labels else gauge
    return target._value.get()


@pytest.fixture
def graph(tmp_path):
    made = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE, lock=SpaceLock())
    yield made
    made.close()


async def test_it_reports_entities_and_both_statement_states(graph) -> None:
    await graph.add_triple(
        subject="用户", predicate="likes", object="绿茶",
        audience="owner", source_turn_id="turn-1",
    )
    await graph.add_triple(
        subject="用户", predicate="lives_in", object="杭州",
        audience="owner", source_turn_id="turn-2",
    )
    await graph.invalidate(subject="用户", predicate="likes", object="绿茶")

    await publish_graph_size(graph, memory_space_id=SPACE)

    # Three entities: 用户, 绿茶, 杭州.
    assert _value(metrics.GRAPH_ENTITIES) == 3
    assert _value(metrics.GRAPH_STATEMENTS, state="active") == 1
    assert _value(metrics.GRAPH_STATEMENTS, state="invalidated") == 1


async def test_an_untouched_graph_reports_zero_rather_than_nothing(graph) -> None:
    """Absent and zero are different answers, and only one of them is useful.

    A space nobody has written to still has to appear in the exposition — a
    missing series reads as "this process is not reporting", which is what an
    operator would conclude right when they are trying to find out whether the
    graph is the reason recalls are timing out.
    """

    await publish_graph_size(graph, memory_space_id=SPACE)

    assert _value(metrics.GRAPH_ENTITIES) == 0
    assert _value(metrics.GRAPH_STATEMENTS, state="active") == 0
    assert b"eidolon_memory_graph_entities" in metrics.render_metrics()


async def test_it_survives_a_graph_that_cannot_answer(graph) -> None:
    """Telemetry must never be the thing that kills the runner."""

    class _Broken:
        async def stats(self):
            raise RuntimeError("database is locked")

    await publish_graph_size(_Broken(), memory_space_id=SPACE)  # must not raise


async def test_no_graph_is_not_an_error() -> None:
    await publish_graph_size(None, memory_space_id=SPACE)


async def test_the_sample_is_paced_by_a_clock_not_by_writes() -> None:
    """The regression this file exists for.

    The first version of these gauges hung off the end of the WAL checkpoint,
    which only runs once ``sync_every`` writes have accumulated. On a space that
    is read often and written rarely — the space most likely to be timing out —
    the series was never published at all. So the sampler must be its own task on
    its own clock, and it must publish once before waiting.
    """

    import inspect

    from eidolon.memory.entrypoints import agent_runner

    source = inspect.getsource(agent_runner._nats_subscriber_loop)
    assert "_publish_forever" in source
    assert "asyncio.create_task(_publish_forever())" in source

    publisher = source.split("async def _publish_forever")[1].split("async def ")[0]
    before_loop, _, after_loop = publisher.partition("while not stop.is_set():")
    assert "publish_graph_size" in before_loop, (
        "the first sample must not wait for the first tick"
    )
    assert "publish_graph_size" in after_loop
    assert "writes_since_checkpoint" not in publisher, (
        "sampling must not be gated on the write counter again"
    )
    assert GRAPH_SAMPLE_SECONDS > 0


async def test_the_sampler_never_runs_inside_the_space_lock(graph) -> None:
    """``stats()`` takes the reader side and the lock is not reentrant.

    Holding the writer and then sampling would deadlock the process outright, so
    this pins the ordering rather than leaving it to a comment.
    """

    async with graph._lock.writer():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                publish_graph_size(graph, memory_space_id=SPACE), timeout=0.2
            )

    # And outside it, the same call returns immediately.
    await asyncio.wait_for(publish_graph_size(graph, memory_space_id=SPACE), timeout=5)


async def test_one_recall_is_counted_once() -> None:
    """It was counted twice, so every rate built on this was 2x.

    ``recall_with_kg_fusion`` records the recall on its way out;
    ``memory_service`` recorded it again. The duplicate also carried
    ``backend="configured"`` — a literal, not the backend's name — so the latency
    histogram grew a second series that described nothing.
    """

    import inspect

    from eidolon.memory.application import memory_service, public_recall

    service = inspect.getsource(memory_service)
    recall = inspect.getsource(public_recall)

    # The degraded path keeps its own increment: an exception means the inner
    # recorder never ran, and that outcome would otherwise never be counted.
    assert service.count("RECALL_TOTAL.labels") == 1
    assert 'outcome="degraded"' in service
    assert "RECALL_SECONDS.labels" not in service, (
        "latency belongs to the layer that knows the real backend name"
    )
    assert recall.count("RECALL_TOTAL.labels") == 1
    assert recall.count("RECALL_SECONDS.labels") == 1


async def test_a_graph_timeout_is_labelled_by_the_caller_not_by_a_threshold() -> None:
    """``kind`` was re-derived from the timeout value the caller had just chosen.

    ``recall_kind`` is computed three lines above the call. Inferring it back out
    of a float meant that changing the voice budget past 100 ms would silently
    relabel every timeout as chat.
    """

    import inspect

    from eidolon.memory.application import public_recall

    source = inspect.getsource(public_recall)

    assert "GRAPH_TIMEOUTS.labels(kind=kind)" in source
    assert "kind=recall_kind" in source
    # The old form, as code rather than as the comment recording it.
    assert 'labels(kind="voice" if' not in source
