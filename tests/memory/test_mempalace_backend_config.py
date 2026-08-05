from __future__ import annotations

from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.mempalace_backend import (
    backend_artifact_path,
    mempalace_backend_env,
    reconcile_configured_backend,
    selected_mempalace_backend,
    vector_sqlite_integrity_targets,
)


def test_default_backend_is_chroma() -> None:
    settings = MemorySettings()
    assert selected_mempalace_backend(settings) == "chroma"
    env = mempalace_backend_env(settings, base={})
    assert env["MEMPALACE_BACKEND"] == "chroma"


def test_backends_we_do_not_run_are_refused() -> None:
    """MemPalace offers five; passing one through would fail later and deeper.

    ``milvus``, ``qdrant`` and ``pgvector`` are server backends and this service
    is local. ``sqlite_exact`` scans every row per query, which the voice path
    cannot absorb.
    """

    for rejected in ("sqlite_exact", "qdrant", "pgvector", "nonsense"):
        settings = MemorySettings.model_validate({"mempalace": {"backend": rejected}})
        with pytest.raises(ValueError, match="unsupported mempalace backend"):
            selected_mempalace_backend(settings)


def test_embedding_env_is_applied() -> None:
    settings = MemorySettings.model_validate(
        {
            "mempalace": {
                "embedding_model": "embeddinggemma",
                "embedding_device": "coreml",
                "embedding_model_dir": "/models/embeddinggemma",
            }
        }
    )

    env = mempalace_backend_env(settings, base={})

    assert settings.mempalace.embedding_model_dir == "/models/embeddinggemma"
    assert env["MEMPALACE_EMBEDDING_MODEL"] == "embeddinggemma"
    assert env["MEMPALACE_EMBEDDING_DEVICE"] == "coreml"
    assert env["MEMPALACE_EMBEDDING_MODEL_DIR"] == "/models/embeddinggemma"


def test_embedding_threads_env_is_applied() -> None:
    settings = MemorySettings.model_validate(
        {"mempalace": {"embedding_threads": 3}}
    )

    env = mempalace_backend_env(settings, base={})

    assert env["MEMPALACE_EMBEDDING_THREADS"] == "3"


def test_embedding_threads_auto_leaves_native_default_unset() -> None:
    settings = MemorySettings()

    env = mempalace_backend_env(settings, base={})

    assert "MEMPALACE_EMBEDDING_THREADS" not in env


def test_backend_artifacts_and_integrity_targets(tmp_path: Path) -> None:
    assert backend_artifact_path(tmp_path, "chroma") == tmp_path / "chroma.sqlite3"

    with pytest.raises(ValueError, match="unsupported mempalace backend"):
        backend_artifact_path(tmp_path, "milvus")

    # The file is local, so its integrity is checkable before serving — which is
    # the reason a corrupt palace is a startup failure rather than a bad read.
    assert vector_sqlite_integrity_targets(tmp_path, "chroma") == [
        ("chroma", tmp_path / "chroma.sqlite3")
    ]


def _sqlite_with_tables(path: Path, *tables: str) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        for table in tables:
            connection.execute(f'CREATE TABLE "{table}" (id TEXT)')
        connection.commit()
    finally:
        connection.close()


def test_empty_selected_artifact_is_reinitialized_candidate(tmp_path: Path) -> None:
    """A zero-byte database cannot hold data, so it is cleared rather than opened.

    Only zero bytes qualifies. Anything larger may hold memories, and deleting it
    to recover from an unreadable file would be the worse outcome of the two.
    """

    selected = tmp_path / "chroma.sqlite3"
    selected.touch()

    report = reconcile_configured_backend(tmp_path, "chroma")

    assert report.state == "uninitialized"
    assert report.removed_artifacts == (str(selected),)
    assert not selected.exists()


def test_the_offline_embedding_matches_the_real_embedder_dimension() -> None:
    """A palace's collection is created with the real embedder's width.

    Initialisation runs the actual model, so a hash vector of any other
    dimension is rejected on the first write — which surfaces as a confusing
    "expecting dimension 384, got N" rather than anything about test mode.
    Both models MemPalace offers emit 384.
    """

    from eidolon.memory.adapters.mempalace_python_backend import (
        _OFFLINE_EMBEDDING_DIM,
        _deterministic_embedding,
    )

    assert _OFFLINE_EMBEDDING_DIM == 384
    assert len(_deterministic_embedding("anything")) == 384


def test_the_offline_embedding_is_stable_and_normalised() -> None:
    """Stable so a rerun sees the same neighbours; normalised for cosine."""

    first = _deterministic_embedding_of("owner likes green")
    again = _deterministic_embedding_of("owner likes green")
    other = _deterministic_embedding_of("something unrelated")

    assert first == again
    assert first != other
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def _deterministic_embedding_of(text: str):
    from eidolon.memory.adapters.mempalace_python_backend import _deterministic_embedding

    return _deterministic_embedding(text)
