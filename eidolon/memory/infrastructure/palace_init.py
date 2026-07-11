"""Shared `mempalace init` helper used by supervisor and agent_runner.

Calling `mempalace init` via :func:`subprocess.run` (not in-process) is intentional:
the supervisor process must never hold a chromadb PersistentClient (forking that
state into agent_runner subprocesses corrupts SQLite). A short-lived subprocess
leaves no chromadb state behind.

The CLI is resolved next to ``sys.executable`` first (so the helper works
regardless of the launching shell's ``PATH``), with a fallback to plain
``mempalace`` lookup.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from eidolon.memory.infrastructure.mempalace_backend import backend_is_initialized
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_MEMPALACE_INIT_TIMEOUT_SECONDS = 300.0
_BACKEND_MATERIALIZE_TIMEOUT_SECONDS = 300.0


class PalaceInitError(RuntimeError):
    """Raised when ``mempalace init`` exits non-zero or times out."""


def _snippet(value: str | bytes | None, limit: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return value.strip()[:limit]


def palace_is_initialized(palace_path: Path, *, backend: str = "chroma") -> bool:
    """Best-effort check for the selected MemPalace backend artifact."""
    return backend_is_initialized(palace_path, backend)


def _resolve_mempalace_cli() -> str:
    """Return the absolute path of the ``mempalace`` CLI bundled with this venv.

    ``sys.executable``'s sibling beats ``PATH`` so that the helper works when
    invoked from a process whose ``PATH`` does not include the venv ``bin``
    (e.g. launchd or a Bash shell that called ``.venv/bin/python`` directly).
    We deliberately do **not** call ``Path.resolve()`` on ``sys.executable``:
    on systems where the venv's ``python`` is a symlink to the system
    interpreter (e.g. Homebrew on macOS), resolving would land us in
    ``/opt/homebrew/.../bin`` where the venv's console-scripts do not live.
    """
    sibling = Path(sys.executable).parent / "mempalace"
    if sibling.is_file():
        return str(sibling)
    # Fallback: PATH lookup (covers exotic venv layouts).
    found = shutil.which("mempalace")
    if found:
        return found
    msg = (
        f"mempalace CLI not found next to {sys.executable} or on PATH; "
        "is the package installed in this environment?"
    )
    raise PalaceInitError(msg)


def ensure_palace_initialized(
    user_id: str,
    palace_path: Path,
    *,
    backend: str = "chroma",
    env: dict[str, str] | None = None,
    timeout_seconds: float = _MEMPALACE_INIT_TIMEOUT_SECONDS,
) -> None:
    """Run ``mempalace init <palace_path>`` if the palace is not yet present.

    The subprocess is isolated from the caller so no chromadb / SQLite state
    leaks into the parent process (critical for supervisor pre-spawn init).
    """
    palace_path = Path(palace_path).expanduser().resolve()
    if palace_is_initialized(palace_path, backend=backend):
        log.debug(
            "palace_init_skip_already_initialized",
            user_id=user_id,
            palace=str(palace_path),
            backend=backend,
        )
        return

    # mempalace init wants the directory to exist
    palace_path.parent.mkdir(parents=True, exist_ok=True)
    palace_path.mkdir(parents=True, exist_ok=True)
    log.info(
        "palace_init_start",
        user_id=user_id,
        palace=str(palace_path),
        backend=backend,
        timeout_seconds=timeout_seconds,
    )
    cli = _resolve_mempalace_cli()
    cmd = [
        cli,
        "--backend",
        backend,
        "init",
        "--backend",
        backend,
        "--yes",
        "--no-llm",
        str(palace_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise PalaceInitError(
            f"mempalace CLI {cli!r} disappeared between resolve and exec"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PalaceInitError(
            f"mempalace init timed out after {timeout_seconds}s for {user_id!r}; "
            f"stdout={_snippet(exc.stdout)!r} stderr={_snippet(exc.stderr)!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PalaceInitError(
            f"mempalace init failed for {user_id!r}: rc={exc.returncode} "
            f"stderr={(exc.stderr or '').strip()[:300]}"
        ) from exc

    # ``mempalace init`` only writes config files. Backend artifacts are created
    # lazily by collection access/write. Materialize them here so the palace is
    # fully ready before agent_runner spawns.
    if not palace_is_initialized(palace_path, backend=backend):
        _materialize_backend_collection(palace_path, backend=backend, env=env)

    if not palace_is_initialized(palace_path, backend=backend):
        raise PalaceInitError(
            f"mempalace init succeeded but {backend} artifact not materialized at {palace_path}"
        )

    log.info(
        "palace_init_ok",
        user_id=user_id,
        palace=str(palace_path),
        backend=backend,
        stdout=(completed.stdout or "").strip()[:200],
    )


def _materialize_backend_collection(
    palace_path: Path,
    *,
    backend: str,
    env: dict[str, str] | None = None,
) -> None:
    """Force-create the selected backend's default drawers collection.

    Run as a subprocess so the parent process never holds backend client state
    (especially chromadb PersistentClient) — the same rationale as
    ``subprocess.run`` for ``mempalace init``.
    """
    code = (
        "import hashlib, math, re, sys; "
        "from eidolon.memory.infrastructure.embedding_model_dir import "
        "apply_local_embedding_model_dir_from_env; "
        "apply_local_embedding_model_dir_from_env(); "
        "from mempalace.palace import get_collection; "
        "palace, backend = sys.argv[1], sys.argv[2]; "
        "col = get_collection(palace, create=True, backend=backend); "
        "probe = '__eidolon_backend_init_probe__'; "
        "doc = 'eidolon backend init probe'; "
        "kwargs = {'ids':[probe], 'documents':[doc], "
        "'metadatas':[{'wing':'Wing_Work','room':'init','source_file':'eidolon'}]}; "
        "\nif backend == 'sqlite_exact':\n"
        "    vec = [0.0] * 64\n"
        "    for token in (re.findall(r'\\w+', doc.lower()) or [doc]):\n"
        "        digest = hashlib.sha256(token.encode()).digest()\n"
        "        idx = digest[0] % 64\n"
        "        sign = 1.0 if digest[1] % 2 == 0 else -1.0\n"
        "        vec[idx] += sign * (1.0 + digest[2] / 255.0)\n"
        "    norm = math.sqrt(sum(v * v for v in vec)) or 1.0\n"
        "    kwargs['embeddings'] = [[v / norm for v in vec]]\n"
        "col.upsert(**kwargs); "
        "col.delete(ids=[probe]); "
        "print('ok')"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, str(palace_path), backend],
            check=False,
            capture_output=True,
            text=True,
            timeout=_BACKEND_MATERIALIZE_TIMEOUT_SECONDS,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise PalaceInitError(
            f"{backend} collection materialization timed out after "
            f"{_BACKEND_MATERIALIZE_TIMEOUT_SECONDS}s "
            f"for {palace_path}: stdout={_snippet(exc.stdout)!r} "
            f"stderr={_snippet(exc.stderr)!r}"
        ) from exc
    if completed.returncode != 0:
        raise PalaceInitError(
            f"{backend} collection materialization failed for {palace_path}: "
            f"rc={completed.returncode} stderr={(completed.stderr or '').strip()[:1200]}"
        )
