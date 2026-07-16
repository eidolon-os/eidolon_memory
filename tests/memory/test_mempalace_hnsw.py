from __future__ import annotations

from eidolon.memory.infrastructure.mempalace_hnsw import probe_hnsw_safety


def test_probe_disables_vector_only_for_confirmed_divergence(monkeypatch) -> None:
    monkeypatch.setattr(
        "mempalace.backends.chroma.hnsw_capacity_status",
        lambda *_args, **_kwargs: {
            "status": "diverged",
            "diverged": True,
            "message": "sqlite is ahead",
        },
    )

    result = probe_hnsw_safety("/tmp/palace")

    assert result.vector_disabled is True
    assert result.status == "diverged"
    assert result.message == "sqlite is ahead"


def test_probe_keeps_vector_enabled_for_unknown_status(monkeypatch) -> None:
    monkeypatch.setattr(
        "mempalace.backends.chroma.hnsw_capacity_status",
        lambda *_args, **_kwargs: {
            "status": "unknown",
            "diverged": False,
            "message": "metadata not flushed",
        },
    )

    result = probe_hnsw_safety("/tmp/palace")

    assert result.vector_disabled is False
    assert result.status == "unknown"


def test_probe_failure_is_fail_open_and_observable(monkeypatch) -> None:
    def _raise(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr("mempalace.backends.chroma.hnsw_capacity_status", _raise)

    result = probe_hnsw_safety("/tmp/palace")

    assert result.vector_disabled is False
    assert result.status == "unknown"
    assert "probe failed" in result.message
