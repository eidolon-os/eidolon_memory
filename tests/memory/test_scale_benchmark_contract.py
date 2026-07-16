"""Long-horizon scale benchmark must exercise production-shaped records."""

from __future__ import annotations

from eidolon.memory.adapters.mempalace_python_backend import _drawer_id
from scripts.bench_mempalace_scale_curve import _doc, _summary


def test_scale_documents_use_production_identity_tenant_and_privacy_metadata() -> None:
    wing, room, text, metadata = _doc(50)

    assert _drawer_id(wing, room, text).startswith("drawer_")
    assert metadata["memory_space_id"] == "default"
    assert metadata["privacy"] == "do_not_recall"


def test_scale_summary_reports_tail_latency() -> None:
    summary = _summary([1.0, 2.0, 100.0])

    assert summary["p95"] == 100.0
    assert summary["p99"] == 100.0
