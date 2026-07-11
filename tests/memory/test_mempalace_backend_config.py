from __future__ import annotations

from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.mempalace_backend import (
    backend_artifact_path,
    backend_is_initialized,
    mempalace_backend_env,
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
