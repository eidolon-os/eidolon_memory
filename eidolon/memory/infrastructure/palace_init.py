"""Shared `mempalace init` helper used by supervisor, agent_runner, init.sh.

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

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class PalaceInitError(RuntimeError):
    """Raised when ``mempalace init`` exits non-zero or times out."""


def palace_is_initialized(palace_path: Path) -> bool:
    """Best-effort check: chroma.sqlite3 present means mempalace has been initialized."""
    return (palace_path / "chroma.sqlite3").is_file()


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
    timeout_seconds: float = 60.0,
) -> None:
    """Run ``mempalace init <palace_path>`` if the palace is not yet present.

    The subprocess is isolated from the caller so no chromadb / SQLite state
    leaks into the parent process (critical for supervisor pre-spawn init).
    """
    palace_path = Path(palace_path).expanduser().resolve()
    if palace_is_initialized(palace_path):
        log.debug(
            "palace_init_skip_already_initialized",
            user_id=user_id,
            palace=str(palace_path),
        )
        return

    # mempalace init wants the directory to exist
    palace_path.parent.mkdir(parents=True, exist_ok=True)
    palace_path.mkdir(parents=True, exist_ok=True)
    log.info("palace_init_start", user_id=user_id, palace=str(palace_path))
    cli = _resolve_mempalace_cli()
    try:
        completed = subprocess.run(
            [cli, "init", "--yes", "--no-llm", str(palace_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise PalaceInitError(f"mempalace CLI {cli!r} disappeared between resolve and exec") from exc
    except subprocess.TimeoutExpired as exc:
        raise PalaceInitError(
            f"mempalace init timed out after {timeout_seconds}s for {user_id!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PalaceInitError(
            f"mempalace init failed for {user_id!r}: rc={exc.returncode} "
            f"stderr={(exc.stderr or '').strip()[:300]}"
        ) from exc

    # ``mempalace init`` only writes ``mempalace.yaml``; chroma.sqlite3 is created
    # lazily by ``get_collection(..., create=True)``. Materialize it here so the
    # palace is fully ready before agent_runner spawns.
    if not palace_is_initialized(palace_path):
        _materialize_chroma_collection(palace_path)

    if not palace_is_initialized(palace_path):
        raise PalaceInitError(
            f"mempalace init succeeded but chroma.sqlite3 not materialized at {palace_path}"
        )

    log.info(
        "palace_init_ok",
        user_id=user_id,
        palace=str(palace_path),
        stdout=(completed.stdout or "").strip()[:200],
    )


def _materialize_chroma_collection(palace_path: Path) -> None:
    """Force-create chroma.sqlite3 + default drawers collection.

    Run as a subprocess so the parent process never holds a chromadb
    PersistentClient — the same rationale as ``subprocess.run`` for
    ``mempalace init``.
    """
    code = (
        "import sys; from mempalace.palace import get_collection; "
        "get_collection(sys.argv[1], create=True); print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(palace_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if completed.returncode != 0:
        raise PalaceInitError(
            f"chroma collection materialization failed for {palace_path}: "
            f"rc={completed.returncode} stderr={(completed.stderr or '').strip()[:300]}"
        )
