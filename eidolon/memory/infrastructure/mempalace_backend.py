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
from eidolon.memory.infrastructure.embedder_factory import (
    EMBEDDING_CONFIG_ENV,
    embedding_config_env_value,
)
from eidolon.memory.infrastructure.embedder_registration import register_embedder
from eidolon.memory.infrastructure.embedding_model_dir import (
    apply_mempalace_model_dir_bridge_from_env,
)

#: One backend, because this deployment is local. MemPalace offers others;
#: they are server backends and serving from several hosts is not a shape we
#: run. Kept as a set rather than inlined so the check reads the same and a
#: second embedded backend would be one entry.
SUPPORTED_MEMPALACE_BACKENDS = frozenset({"chroma"})
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


#: Variables this module owns in a child's environment. The MemPalace-prefixed
#: ones are theirs to read; ``EIDOLON_EMBEDDING_CONFIG`` is the whole embedding
#: section, which a spawned process needs because the encoder is ours and its
#: settings no longer fit into four strings MemPalace happens to understand.
_EXPORTED_ENV_PREFIXES = ("MEMPALACE_", EMBEDDING_CONFIG_ENV)


def mempalace_backend_env(
    settings: MemorySettings,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment with backend and embedder selection applied.

    The four ``MEMPALACE_EMBEDDING_*`` variables are still written, because
    MemPalace reads them itself and because the model name among them is both the
    key its embedder cache is looked up under and the identity it records on the
    palace. They are derived from the ``embedding`` section, which is where the
    embedder is configured now.

    ``EIDOLON_EMBEDDING_CONFIG`` carries the section entire. A child process gets
    a hosted endpoint's address, timeout and declared width from it without this
    function growing a variable per field — and it is the per-field list that
    someone eventually forgets to extend.
    """
    # ``is None`` rather than a truthiness check: ``base={}`` means "start from
    # nothing", and treating it as "not given" silently returned the whole ambient
    # environment instead. The tests that pass an empty base to assert a variable
    # is *absent* were therefore reading this machine's environment, so they would
    # have passed or failed by what happened to be exported.
    env = dict(os.environ if base is None else base)
    backend = selected_mempalace_backend(settings)
    env["MEMPALACE_BACKEND"] = backend

    embedding = settings.embedding
    embedding_model = embedding.model.strip().lower()
    if embedding_model:
        env["MEMPALACE_EMBEDDING_MODEL"] = embedding_model
    embedding_device = embedding.device.strip().lower()
    if embedding_device:
        env["MEMPALACE_EMBEDDING_DEVICE"] = embedding_device
    model_dir = embedding.model_dir.strip()
    if model_dir:
        env["MEMPALACE_EMBEDDING_MODEL_DIR"] = str(Path(model_dir).expanduser())
    if embedding.threads > 0:
        env["MEMPALACE_EMBEDDING_THREADS"] = str(embedding.threads)
    env[EMBEDDING_CONFIG_ENV] = embedding_config_env_value(embedding)

    return env


def apply_mempalace_backend_env(settings: MemorySettings) -> str | None:
    """Apply backend selection to the current process, and install our embedder.

    Returns the embedding model that was installed, or ``None`` when the
    configured model is one of MemPalace's own.

    The embedder registration lives here rather than in its own hook because its
    ordering requirement is identical — after the environment is applied, before
    any store is opened — and because this function has six call sites, five of
    them benchmark scripts. A separate hook would have to be remembered at each,
    and forgetting it in a bench is precisely the defect that made every quality
    number this project published before 2026-08-03 measure the wrong model.
    """
    env = mempalace_backend_env(settings)
    for key, value in env.items():
        if key.startswith(_EXPORTED_ENV_PREFIXES):
            os.environ[key] = value
    # Registered from the settings rather than by re-reading what was just
    # written: the round trip through the environment is the transport for a
    # *child* process, and using it here too would mean a parsing bug showed up
    # only in the parent, where it is hardest to attribute.
    return register_embedder(settings.embedding)


def prepare_embedder_resolution_from_env() -> str | None:
    """Make this process able to resolve the embedder its environment names.

    Two steps, and doing only one of them is a silent wrong answer, which is why
    they live in a single function: bridge a local model directory into
    MemPalace's own hub calls, and install our encoder when the configured one is
    not theirs.

    Needed by any process that opens or creates a collection — including the
    subprocess that materialises a fresh palace, which runs a ``python -c`` and
    therefore inherits none of the parent's registration. Without this there, a
    new palace is created with MemPalace's default encoder while its marker
    records the configured name: minilm vectors under a label saying otherwise,
    and nothing reports a problem.
    """

    apply_mempalace_model_dir_bridge_from_env()
    return register_embedder()


def backend_artifact_path(palace_path: Path, backend: str) -> Path:
    """The file whose presence says this palace was built with ``backend``.

    For Chroma that is its own database, which also carries the collections — so
    a palace with the file but no collections is a distinguishable state, and
    ``inspect_backend_artifact`` reports it rather than treating the file as
    proof.
    """

    if backend == "chroma":
        return palace_path / "chroma.sqlite3"
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
    # The two "foreign" branches below cannot fire while one backend is
    # supported: the set has a single member, so there is no other artifact to
    # find. They are written over the set rather than over a pair of names, so
    # they come back with a second entry — but until there is one, the state this
    # function really distinguishes is whether the configured store is valid,
    # empty, or unreadable. Said here because a reader would otherwise take the
    # conflict handling for protection that is currently active.
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
