"""MemPalace backend selection helpers.

Keeps Eidolon deployment code from assuming every palace is backed by
Chroma's local ``chroma.sqlite3`` file. MemPalace still owns the storage
contract; this module only centralizes process environment and artifact
checks for the supported backends we may run in development.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.embedding_model_dir import (
    apply_local_embedding_model_dir_from_env,
)

SUPPORTED_MEMPALACE_BACKENDS = frozenset({"chroma", "milvus"})
_SQLITE_REQUIRED_TABLES = {
    "chroma": frozenset({"collections", "embeddings"}),
}


@dataclass(frozen=True)
class BackendArtifactStatus:
    backend: str
    path: Path
    state: str
    size_bytes: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "path": str(self.path),
            "state": self.state,
            "size_bytes": self.size_bytes,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BackendArtifactReport:
    configured_backend: str
    state: str
    issue: str
    artifacts: tuple[BackendArtifactStatus, ...]
    removed_artifacts: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured_backend": self.configured_backend,
            "backend_state": self.state,
            "backend_issue": self.issue,
            "backend_artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "removed_backend_artifacts": list(self.removed_artifacts),
        }


class BackendArtifactError(RuntimeError):
    """Configured backend cannot safely open the current Palace artifacts."""

    def __init__(self, report: BackendArtifactReport) -> None:
        self.report = report
        super().__init__(report.issue)


def selected_mempalace_backend(settings: MemorySettings) -> str:
    backend = (settings.mempalace.backend or "chroma").strip().lower()
    if backend not in SUPPORTED_MEMPALACE_BACKENDS:
        available = ", ".join(sorted(SUPPORTED_MEMPALACE_BACKENDS))
        raise ValueError(f"unsupported mempalace backend {backend!r}; available: {available}")
    return backend


def mempalace_backend_env(
    settings: MemorySettings,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment with MemPalace backend selection applied."""
    env = dict(base or os.environ)
    backend = selected_mempalace_backend(settings)
    env["MEMPALACE_BACKEND"] = backend

    embedding_model = settings.mempalace.embedding_model.strip().lower()
    if embedding_model:
        env["MEMPALACE_EMBEDDING_MODEL"] = embedding_model
    embedding_device = settings.mempalace.embedding_device.strip().lower()
    if embedding_device:
        env["MEMPALACE_EMBEDDING_DEVICE"] = embedding_device
    embedding_model_dir = settings.mempalace.embedding_model_dir.strip()
    if embedding_model_dir:
        env["MEMPALACE_EMBEDDING_MODEL_DIR"] = str(Path(embedding_model_dir).expanduser())
    if settings.mempalace.embedding_threads > 0:
        env["MEMPALACE_EMBEDDING_THREADS"] = str(settings.mempalace.embedding_threads)

    if backend == "milvus":
        env.update(_milvus_env(settings))

    return env


def _milvus_env(settings: MemorySettings) -> dict[str, str]:
    """Milvus connection settings, as MemPalace's environment contract.

    MemPalace reads these rather than taking arguments, so this is the one place
    that translates our config into its vocabulary.

    ``MEMPALACE_MILVUS_DB_NAME`` is the important one for a server: it confines
    every collection this deployment creates to a named database, leaving the
    rest of the instance alone. The settings model requires it whenever a uri is
    set, so reaching here without one means Milvus Lite against a local file.
    """

    cfg = settings.mempalace
    env: dict[str, str] = {}

    uri = cfg.milvus_uri.strip()
    if uri:
        env["MEMPALACE_MILVUS_URI"] = uri
    db_name = cfg.milvus_db_name.strip()
    if db_name:
        env["MEMPALACE_MILVUS_DB_NAME"] = db_name
    namespace = cfg.milvus_namespace.strip()
    if namespace:
        env["MEMPALACE_MILVUS_NAMESPACE"] = namespace
    token = cfg.resolve_milvus_token()
    if token:
        env["MEMPALACE_MILVUS_TOKEN"] = token
    return env


def apply_mempalace_backend_env(settings: MemorySettings) -> None:
    """Apply backend selection to the current process."""
    env = mempalace_backend_env(settings)
    for key, value in env.items():
        if key.startswith("MEMPALACE_"):
            os.environ[key] = value
    apply_local_embedding_model_dir_from_env()


def backend_artifact_path(palace_path: Path, backend: str) -> Path:
    """The file whose presence says this palace was built with ``backend``.

    Chroma's is its database. Milvus stores vectors remotely, but MemPalace still
    leaves a marker recording which uri and database the palace was bound to, so
    a changed target is caught instead of silently creating a second, empty
    collection set.
    """

    if backend == "chroma":
        return palace_path / "chroma.sqlite3"
    if backend == "milvus":
        return palace_path / "milvus_backend.json"
    raise ValueError(f"unsupported mempalace backend {backend!r}")


def backend_is_initialized(palace_path: Path, backend: str) -> bool:
    return inspect_backend_artifact(palace_path, backend).state == "valid"


def inspect_backend_artifact(
    palace_path: Path,
    backend: str,
) -> BackendArtifactStatus:
    """Inspect one backend marker read-only; never creates a missing artifact."""
    path = backend_artifact_path(Path(palace_path), backend)
    if not path.is_file():
        return BackendArtifactStatus(backend, path, "absent")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return BackendArtifactStatus(
            backend, path, "invalid", detail=f"{type(exc).__name__}: {exc}"
        )
    if size == 0:
        return BackendArtifactStatus(backend, path, "invalid", 0, "empty artifact")
    if backend in _SQLITE_REQUIRED_TABLES:
        state, detail = _inspect_sqlite_artifact(path, _SQLITE_REQUIRED_TABLES[backend])
    else:
        state, detail = _inspect_json_artifact(path)
    return BackendArtifactStatus(backend, path, state, size, detail)


def inspect_configured_backend(
    palace_path: Path,
    configured_backend: str,
) -> BackendArtifactReport:
    """Validate disk artifacts against the configured source of truth."""
    configured = configured_backend.strip().lower()
    if configured not in SUPPORTED_MEMPALACE_BACKENDS:
        return BackendArtifactReport(
            configured,
            "invalid",
            f"unsupported configured backend {configured!r}",
            (),
        )
    artifacts = tuple(
        inspect_backend_artifact(palace_path, backend)
        for backend in sorted(SUPPORTED_MEMPALACE_BACKENDS)
    )
    selected = next(a for a in artifacts if a.backend == configured)
    foreign_valid = [
        a.backend for a in artifacts if a.backend != configured and a.state == "valid"
    ]
    foreign_invalid = [
        a for a in artifacts if a.backend != configured and a.state == "invalid"
    ]
    if foreign_valid:
        return BackendArtifactReport(
            configured,
            "conflict",
            f"configured backend {configured!r} conflicts with valid artifacts: "
            f"{', '.join(foreign_valid)}",
            artifacts,
        )
    if selected.state == "invalid":
        return BackendArtifactReport(
            configured,
            "invalid",
            f"configured backend {configured!r} artifact is invalid: {selected.detail}",
            artifacts,
        )
    if foreign_invalid:
        return BackendArtifactReport(
            configured,
            "stale_artifact",
            "; ".join(f"{a.backend}: {a.detail}" for a in foreign_invalid),
            artifacts,
        )
    if selected.state == "valid":
        return BackendArtifactReport(configured, "ready", "", artifacts)
    return BackendArtifactReport(
        configured,
        "uninitialized",
        f"configured backend {configured!r} has no initialized artifact",
        artifacts,
    )


def reconcile_configured_backend(
    palace_path: Path,
    configured_backend: str,
    *,
    remove_empty_artifacts: bool = True,
) -> BackendArtifactReport:
    """Clean provably empty artifacts, then enforce configured-backend consistency.

    Only zero-byte files are removed automatically because they cannot contain
    backend data. Non-empty foreign or malformed artifacts always fail closed.
    """
    palace = Path(palace_path)
    report = inspect_configured_backend(palace, configured_backend)
    removed: list[str] = []
    if remove_empty_artifacts:
        for artifact in report.artifacts:
            if artifact.state != "invalid" or artifact.size_bytes != 0:
                continue
            try:
                artifact.path.unlink()
            except FileNotFoundError:
                pass
            else:
                removed.append(str(artifact.path))
        if removed:
            report = replace(
                inspect_configured_backend(palace, configured_backend),
                removed_artifacts=tuple(removed),
            )
    if report.state in {"conflict", "invalid", "stale_artifact"}:
        raise BackendArtifactError(report)
    return report


def _inspect_sqlite_artifact(
    path: Path,
    required_tables: frozenset[str],
) -> tuple[str, str]:
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            if not row or str(row[0]).strip().lower() != "ok":
                return "invalid", f"quick_check={row[0] if row else 'no result'}"
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return "invalid", f"{type(exc).__name__}: {exc}"
    missing = sorted(required_tables - tables)
    if missing:
        return "invalid", f"missing required tables: {', '.join(missing)}"
    return "valid", ""


def _inspect_json_artifact(path: Path) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return "invalid", f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return "invalid", "marker must contain a JSON object"
    return "valid", ""


def vector_sqlite_integrity_targets(palace_path: Path, backend: str) -> list[tuple[str, Path]]:
    """Local vector-store SQLite files that should pass integrity_check.

    Empty for remote backends: their storage is the server's to verify, and there
    is no local file whose corruption should stop this process from starting.
    """

    if backend == "chroma":
        return [("chroma", palace_path / "chroma.sqlite3")]
    return []
