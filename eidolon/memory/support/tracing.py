"""Follow one turn through the service.

A trace id already travels from the channel through the agent to here, but it was
only ever logged — there was no way to see which part of a recall took the time,
or which of its parallel signals failed. This adds spans over that existing id.

Not OpenTelemetry, but shaped like it. A span emits ``trace_id``, ``span_id``,
``parent_span_id`` and ``duration_ms`` as structured log fields, so a local
deployment reads them with grep while a future exporter can map them without any
call site changing. Taking the SDK instead would mean an API, an SDK and an
exporter as hard dependencies, plus a collector to receive what they produce —
infrastructure a laptop does not have.

Spans on the voice path are sampled, because the log write is the expensive part
of recording one and voice has the least room. Metrics are always recorded: an
observation costs a microsecond or two, so there is nothing to sample away.
"""

from __future__ import annotations

import contextvars
import os
import random
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

# The current span, so a nested one can name its parent without every function
# having to thread it through. A contextvar rather than a global because tasks
# for different spaces run concurrently in one loop.
_CURRENT: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "eidolon_memory_span", default=None
)


def new_trace_id() -> str:
    """A trace id for work that arrived without one."""

    return uuid.uuid4().hex


def _new_span_id() -> str:
    # Eight bytes, like OTel, so ids stay recognisable if these are ever exported.
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str | None:
    current = _CURRENT.get()
    return current[0] if current else None


@contextmanager
def span(
    name: str,
    *,
    trace_id: str | None = None,
    sample: float = 1.0,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Time a unit of work and emit it as one structured event.

    Yields a dict for attributes discovered while the work runs — a result count,
    a degraded flag — which are emitted with the span rather than needing a second
    log line to correlate.

    Never swallows an exception. A failing span records what it knows and
    re-raises, because a span that hid a failure would be worse than no span.
    """

    parent = _CURRENT.get()
    resolved_trace = trace_id or (parent[0] if parent else new_trace_id())
    span_id = _new_span_id()
    token = _CURRENT.set((resolved_trace, span_id))

    attributes: dict[str, Any] = {}
    started = time.perf_counter()
    failed: BaseException | None = None
    try:
        yield attributes
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        failed = exc
        raise
    finally:
        _CURRENT.reset(token)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        # A failed span is always emitted: sampling exists to keep the happy path
        # cheap, and dropping failures is how an incident becomes invisible.
        if failed is not None or _should_record(sample):
            log.info(
                name,
                trace_id=resolved_trace,
                span_id=span_id,
                parent_span_id=parent[1] if parent else None,
                duration_ms=elapsed_ms,
                error=type(failed).__name__ if failed else None,
                **fields,
                **attributes,
            )


def _should_record(sample: float) -> bool:
    if sample >= 1.0:
        return True
    if sample <= 0.0:
        return False
    return random.random() < sample


def voice_sample_rate() -> float:
    """How often to record a span on the voice path.

    Overridable because the right rate depends on traffic: debugging one
    deployment wants everything, a busy one wants a fraction.
    """

    raw = os.environ.get("EIDOLON_MEMORY_TRACE_SAMPLE", "").strip()
    if not raw:
        return 0.05
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.05
