from __future__ import annotations

from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.mempalace_backend import (
    BackendArtifactError,
    backend_artifact_path,
    backend_is_initialized,
    inspect_configured_backend,
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


def test_qdrant_env_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_KEY_FOR_TEST", "secret")
    settings = MemorySettings.model_validate(
        {
            "mempalace": {
                "backend": "qdrant",
                "qdrant_url": "http://127.0.0.1:6333",
                "qdrant_namespace": "ab-test",
                "qdrant_timeout_seconds": 2.5,
                "qdrant_api_key_env": "QDRANT_KEY_FOR_TEST",
            }
        }
    )

    env = mempalace_backend_env(settings, base={})

    assert env["MEMPALACE_BACKEND"] == "qdrant"
    assert env["MEMPALACE_QDRANT_URL"] == "http://127.0.0.1:6333"
    assert env["MEMPALACE_QDRANT_NAMESPACE"] == "ab-test"
    assert env["MEMPALACE_QDRANT_TIMEOUT"] == "2.5"
    assert env["MEMPALACE_QDRANT_API_KEY"] == "secret"


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
    assert backend_artifact_path(tmp_path, "qdrant") == tmp_path / "qdrant_backend.json"
    assert backend_artifact_path(tmp_path, "sqlite_exact") == tmp_path / "sqlite_exact.sqlite3"

    (tmp_path / "qdrant_backend.json").write_text("{}", encoding="utf-8")
    assert backend_is_initialized(tmp_path, "qdrant")

    assert vector_sqlite_integrity_targets(tmp_path, "qdrant") == []
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


def test_empty_foreign_artifact_is_invalid_and_removed(tmp_path: Path) -> None:
    _sqlite_with_tables(tmp_path / "chroma.sqlite3", "collections", "embeddings")
    stale = tmp_path / "sqlite_exact.sqlite3"
    stale.touch()

    before = inspect_configured_backend(tmp_path, "chroma")
    assert before.state == "stale_artifact"

    after = reconcile_configured_backend(tmp_path, "chroma")
    assert after.ready is True
    assert after.removed_artifacts == (str(stale),)
    assert not stale.exists()


def test_valid_foreign_backend_fails_closed(tmp_path: Path) -> None:
    _sqlite_with_tables(tmp_path / "chroma.sqlite3", "collections", "embeddings")
    _sqlite_with_tables(
        tmp_path / "sqlite_exact.sqlite3", "collections", "documents"
    )

    with pytest.raises(BackendArtifactError) as exc_info:
        reconcile_configured_backend(tmp_path, "chroma")

    assert exc_info.value.report.state == "conflict"
    assert "sqlite_exact" in str(exc_info.value)


def test_nonempty_invalid_foreign_artifact_is_not_deleted(tmp_path: Path) -> None:
    _sqlite_with_tables(tmp_path / "chroma.sqlite3", "collections", "embeddings")
    stale = tmp_path / "sqlite_exact.sqlite3"
    stale.write_bytes(b"not a sqlite database")

    with pytest.raises(BackendArtifactError) as exc_info:
        reconcile_configured_backend(tmp_path, "chroma")

    assert exc_info.value.report.state == "stale_artifact"
    assert stale.read_bytes() == b"not a sqlite database"


def test_empty_selected_artifact_is_reinitialized_candidate(tmp_path: Path) -> None:
    selected = tmp_path / "sqlite_exact.sqlite3"
    selected.touch()

    report = reconcile_configured_backend(tmp_path, "sqlite_exact")

    assert report.state == "uninitialized"
    assert report.removed_artifacts == (str(selected),)
    assert not selected.exists()
