"""Long-horizon scale benchmark must exercise production-shaped records."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.mempalace_python_backend import _drawer_id
from scripts.bench_mempalace_scale_curve import (
    MIXED_LOAD_MODEL,
    _doc,
    _measure_point,
    _parse_concurrencies,
    _summary,
)


def test_concurrency_benchmark_uses_current_actor_context_protocol() -> None:
    import inspect

    from scripts.bench_mempalace_concurrency import _run_search

    source = inspect.getsource(_run_search)
    assert "context=MemoryActorContext" in source
    assert "user_id=" not in source


def test_concurrency_benchmark_seeds_production_tenant_metadata() -> None:
    import inspect

    from scripts.bench_mempalace_concurrency import _seed

    source = inspect.getsource(_seed)
    for field in (
        "memory_space_id",
        "memory_realm_id",
        "wing",
        "privacy",
        "scope",
        "visibility",
    ):
        assert f'"{field}"' in source


def test_scale_documents_use_production_identity_tenant_and_privacy_metadata() -> None:
    wing, room, text, metadata = _doc(50)

    assert _drawer_id(wing, room, text).startswith("drawer_")
    assert metadata["memory_space_id"] == "default"
    assert metadata["privacy"] == "do_not_recall"


def test_scale_summary_reports_tail_latency() -> None:
    summary = _summary([1.0, 2.0, 100.0])

    assert summary["p95"] == 100.0
    assert summary["p99"] == 100.0


def test_scale_concurrency_curve_is_positive_ordered_and_deduplicated() -> None:
    import inspect

    assert MIXED_LOAD_MODEL == "closed_loop_bounded_outstanding"
    assert _parse_concurrencies("8,1,4,2,4") == [1, 2, 4, 8]

    source = inspect.getsource(_measure_point)
    mixed = source[source.index("async def mixed_one") : source.index("await asyncio.gather")]
    assert mixed.index("async with sem") < mixed.index("await _timed")

    with pytest.raises(ValueError, match="positive"):
        _parse_concurrencies("1,0,4")

    with pytest.raises(ValueError, match="at least one"):
        _parse_concurrencies("")
