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


def test_only_the_two_deployment_shapes_are_accepted() -> None:
    """Backends MemPalace offers but we do not run must be refused, not passed through."""

    for rejected in ("sqlite_exact", "qdrant", "pgvector", "nonsense"):
        settings = MemorySettings.model_validate({"mempalace": {"backend": rejected}})
        with pytest.raises(ValueError, match="unsupported mempalace backend"):
            selected_mempalace_backend(settings)


def test_milvus_env_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MILVUS_TOKEN_FOR_TEST", "secret")
    settings = MemorySettings.model_validate(
        {
            "mempalace": {
                "backend": "milvus",
                "milvus_uri": "http://milvus.internal:19530",
                "milvus_db_name": "eidolon",
                "milvus_namespace": "ab-test",
                "milvus_token_env": "MILVUS_TOKEN_FOR_TEST",
            }
        }
    )

    env = mempalace_backend_env(settings, base={})

    assert env["MEMPALACE_BACKEND"] == "milvus"
    assert env["MEMPALACE_MILVUS_URI"] == "http://milvus.internal:19530"
    assert env["MEMPALACE_MILVUS_DB_NAME"] == "eidolon"
    assert env["MEMPALACE_MILVUS_NAMESPACE"] == "ab-test"
    assert env["MEMPALACE_MILVUS_TOKEN"] == "secret"


def test_a_remote_milvus_must_name_its_database() -> None:
    """Without a database name, collections land in the instance default.

    That silently mixes this deployment's data in with whatever else lives on
    the server, so it is refused at config load rather than discovered later.
    """

    with pytest.raises(ValueError, match="milvus_db_name"):
        MemorySettings.model_validate(
            {"mempalace": {"backend": "milvus", "milvus_uri": "http://milvus.internal:19530"}}
        )


def test_milvus_without_a_uri_is_allowed_for_local_lite_use() -> None:
    settings = MemorySettings.model_validate({"mempalace": {"backend": "milvus"}})

    env = mempalace_backend_env(settings, base={})

    assert env["MEMPALACE_BACKEND"] == "milvus"
    assert "MEMPALACE_MILVUS_URI" not in env


def test_milvus_token_is_read_from_the_environment_not_the_file() -> None:
    """Secrets live in the environment; config only names the variable."""

    settings = MemorySettings.model_validate(
        {"mempalace": {"backend": "milvus", "milvus_token_env": "ABSENT_TOKEN_VAR"}}
    )

    assert settings.mempalace.resolve_milvus_token() == ""
    assert "MEMPALACE_MILVUS_TOKEN" not in mempalace_backend_env(settings, base={})


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
    assert backend_artifact_path(tmp_path, "milvus") == tmp_path / "milvus_backend.json"

    (tmp_path / "milvus_backend.json").write_text("{}", encoding="utf-8")
    assert backend_is_initialized(tmp_path, "milvus")

    # A remote store's integrity is the server's business, and there is no local
    # file whose corruption should stop this process from starting.
    assert vector_sqlite_integrity_targets(tmp_path, "milvus") == []
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
    stale = tmp_path / "milvus_backend.json"
    stale.touch()

    before = inspect_configured_backend(tmp_path, "chroma")
    assert before.state == "stale_artifact"

    after = reconcile_configured_backend(tmp_path, "chroma")
    assert after.ready is True
    assert after.removed_artifacts == (str(stale),)
    assert not stale.exists()


def test_valid_foreign_backend_fails_closed(tmp_path: Path) -> None:
    """Two usable backends in one palace means we cannot tell which holds the data."""

    _sqlite_with_tables(tmp_path / "chroma.sqlite3", "collections", "embeddings")
    (tmp_path / "milvus_backend.json").write_text('{"uri": "http://x:19530"}', encoding="utf-8")

    with pytest.raises(BackendArtifactError) as exc_info:
        reconcile_configured_backend(tmp_path, "chroma")

    assert exc_info.value.report.state == "conflict"
    assert "milvus" in str(exc_info.value)


def test_nonempty_invalid_foreign_artifact_is_not_deleted(tmp_path: Path) -> None:
    """Only a zero-byte artifact is safe to remove; anything else may hold data."""

    _sqlite_with_tables(tmp_path / "chroma.sqlite3", "collections", "embeddings")
    stale = tmp_path / "milvus_backend.json"
    stale.write_bytes(b"not json")

    with pytest.raises(BackendArtifactError) as exc_info:
        reconcile_configured_backend(tmp_path, "chroma")

    assert exc_info.value.report.state == "stale_artifact"
    assert stale.read_bytes() == b"not json"


def test_empty_selected_artifact_is_reinitialized_candidate(tmp_path: Path) -> None:
    selected = tmp_path / "milvus_backend.json"
    selected.touch()

    report = reconcile_configured_backend(tmp_path, "milvus")

    assert report.state == "uninitialized"
    assert report.removed_artifacts == (str(selected),)
    assert not selected.exists()
