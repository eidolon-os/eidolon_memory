"""What this service reports about itself.

Recall sits on the critical path of a reply, so the questions an operator needs
answered are about latency and where it went: which of recall's parallel signals
was slow, how often the graph timed out, whether a result was degraded and why.
Counts of stored memories matter far less — those are visible in the store.

**One exception, added 2026-08-06 after the sentence above proved too broad.**
The graph's size is not bookkeeping, it is the independent variable of a latency
that fails silently: ``GRAPH_TIMEOUTS`` says the graph was dropped from a recall
and its own docstring says a climbing rate is the signal, but there was no series
that could say *why* it climbed. "Visible in the store" is true and useless — it
means someone must already suspect the graph, open the file, and count. The four
``GRAPH_*`` gauges below exist so the correlation can be seen instead of guessed.

Prometheus rather than OpenTelemetry, deliberately. A local deployment has no
collector to send traces to, and the supervisor already knows every worker's
address, so pull-based scraping needs no infrastructure that does not exist.
Spans are recorded as structured log events with OTel-compatible field names
(see :mod:`eidolon.memory.support.tracing`), which keeps the option of exporting
them later without changing a single call site.

Metrics are optional at runtime. prometheus_client may be absent — a client that
only speaks the protocol has no reason to install it — so every helper here works
against a no-op when it is. Nothing in the service branches on whether metrics
are available.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised by whichever branch the environment has
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
    from prometheus_client import generate_latest as _generate_latest

    METRICS_AVAILABLE = True
except ImportError:  # pragma: no cover
    METRICS_AVAILABLE = False

    class _Missing:
        """Accepts every call a metric would and does nothing.

        So that a deployment without prometheus_client runs identically rather
        than needing `if metrics_enabled` around every observation.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...
        def labels(self, *args: Any, **kwargs: Any) -> _Missing:
            return self

        def observe(self, *args: Any, **kwargs: Any) -> None: ...
        def inc(self, *args: Any, **kwargs: Any) -> None: ...
        def set(self, *args: Any, **kwargs: Any) -> None: ...

    Counter = Gauge = Histogram = _Missing  # type: ignore[assignment,misc]
    CollectorRegistry = _Missing  # type: ignore[assignment,misc]

    def _generate_latest(registry: Any = None) -> bytes:  # type: ignore[misc]
        return b""


# Buckets chosen around the budgets recall actually has to meet: a voice reply
# has roughly 100ms for retrieval and a chat reply roughly 200ms, so the
# resolution is where those decisions are made rather than spread evenly. The
# tail above one second exists to make "something is badly wrong" visible, not
# to measure it precisely.
_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0,
)

RECALL_SECONDS = Histogram(
    "eidolon_memory_recall_seconds",
    "End-to-end recall latency.",
    ("kind", "backend", "graph", "degraded"),
    buckets=_LATENCY_BUCKETS,
)
"""``kind`` separates voice from chat: they have different budgets, so mixing
them into one distribution would hide both."""

RECALL_STAGE_SECONDS = Histogram(
    "eidolon_memory_recall_stage_seconds",
    "Time inside one stage of a recall.",
    ("stage",),
    buckets=_LATENCY_BUCKETS,
)
"""Recall runs several signals in parallel; without per-stage timing a slow total
says nothing about which one to look at."""

RECALL_TOTAL = Counter(
    "eidolon_memory_recall_total",
    "Recalls served.",
    ("kind", "outcome"),
)
"""``outcome`` distinguishes a recall that found nothing from one that failed —
they look the same to a caller but mean opposite things to an operator."""

GRAPH_TIMEOUTS = Counter(
    "eidolon_memory_graph_timeout_total",
    "Graph lookups abandoned because they exceeded their budget.",
    ("kind",),
)
"""Expected to be non-zero: dropping the graph contribution is the designed
behaviour when it is slow. A rate that climbs is the signal."""

TURN_STAGE_SECONDS = Histogram(
    "eidolon_memory_turn_stage_seconds",
    "Time inside one stage of absorbing a turn.",
    ("stage",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0),
)
"""Wider buckets than recall: this path waits on an LLM, so seconds are normal
and the useful question is which stage dominates."""

TURNS_TOTAL = Counter(
    "eidolon_memory_turn_total",
    "Turns absorbed.",
    ("outcome",),
)

FRAGMENTS_EXTRACTED = Counter(
    "eidolon_memory_fragments_extracted_total",
    "Fragments the steward produced, and what happened to each.",
    ("stage",),
)
"""Where extraction loses material, which was previously unobservable.

``stage`` is one of:

* ``proposed`` — the model returned it
* ``dropped_importance`` — below ``steward.min_importance_to_write``
* ``dropped_cap`` — beyond ``steward.max_fragments_per_turn``
* ``written`` — reached storage

Only the last was visible before, so a corpus that yields few memories looked
identical whether the model proposed little or the thresholds discarded most of
it — and those call for completely different fixes. The probe measured 7
fragments from 40 turns without being able to say which.
"""

SPACES_HELD = Gauge(
    "eidolon_memory_spaces_held",
    "Memory spaces this process currently has open.",
)
"""How the resident embedding model is being amortised. One space per process
means paying for a model per space; this is the number that says whether that is
still happening."""

SPACE_OPEN_SECONDS = Histogram(
    "eidolon_memory_space_open_seconds",
    "Time to open a space: locking, checks, and storage handles.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

SPACE_LOCK_WAIT_SECONDS = Histogram(
    "eidolon_memory_space_lock_wait_seconds",
    "Time an operation waited for a space's readers-writer lock, by side.",
    ("mode",),
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
"""Where recall latency goes when it is not the store's fault.

The graph lookup in ``recall_with_kg_fusion`` runs on a 50ms budget on the voice
path and shares this lock with the vector search it runs alongside. Under the
exclusive mutex this replaced, that budget could be spent entirely waiting — and
the result was reported as a graph timeout, which reads as "the graph is slow"
rather than "the graph never started". The ``read`` series is what distinguishes
those two, and it is the number to look at before tuning any recall timeout."""

GRAPH_ENTITIES = Gauge(
    "eidolon_memory_graph_entities",
    "Entity rows in this space's graph.",
)
"""The independent variable behind ``GRAPH_TIMEOUTS``.

Entities and not statements, which is counter-intuitive enough to be worth
stating: the one graph read that still grows with the graph is entity-name
matching, and it scans ``kg_entities``. Measured on a 60 000-statement graph,
deleting 21% of the statements moved that read by nothing at all, while removing
the orphaned entities moved it by a quarter. A statement count that climbs is
information; this is the number that predicts a timeout.

Nothing has ever deleted an entity row — ``_upsert_entity`` is
``INSERT OR IGNORE`` and there is no counterpart — so this series only rises.
That is the point of having it."""

GRAPH_STATEMENTS = Gauge(
    "eidolon_memory_graph_statements",
    "Statement rows in this space's graph, by whether they are still valid.",
    ("state",),
)
"""``active`` and ``invalidated`` separately, because their sum is the file's
size and their ratio is a product signal: a graph that is mostly invalidated is
one whose owner keeps correcting it."""

GRAPH_WAL_PAGES = Gauge(
    "eidolon_memory_graph_wal_pages",
    "Pages in the graph's write-ahead log at the last checkpoint attempt.",
)
"""Paired with ``GRAPH_CHECKPOINT_PAGES``, and only meaningful next to it.

A single reader that never drains its cursor holds the checkpoint at its
snapshot: the WAL then grows without bound while the main database file stays
frozen, and every checkpoint reports success having moved nothing. That failure
is invisible in either number alone — this one climbing while the counter stays
flat is the whole signal. On an SD card it is also the worst way to fail."""

GRAPH_CHECKPOINT_PAGES = Counter(
    "eidolon_memory_graph_checkpoint_pages_total",
    "Graph WAL pages merged back into the main database file.",
)
"""See ``GRAPH_WAL_PAGES``. A counter rather than a gauge because the question is
whether progress is being made at all, not how much any one attempt made."""


def render_metrics() -> bytes:
    """The exposition payload, or empty when metrics are unavailable."""

    return _generate_latest()


def metrics_content_type() -> str:
    if not METRICS_AVAILABLE:  # pragma: no cover - trivial branch
        return "text/plain; charset=utf-8"
    from prometheus_client import CONTENT_TYPE_LATEST

    return CONTENT_TYPE_LATEST
