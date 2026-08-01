"""Per-Realm temporary-directory isolation for embedded storage processes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from eidolon_memory_contracts import memory_space_storage_name, validate_memory_space_id

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def resolve_process_temp_root(settings: MemorySettings, palace_path: Path) -> Path:
    """Resolve env > config > Palace-adjacent temporary root."""

    env = os.environ.get("EIDOLON_MEMORY_PROCESS_TMP_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.process_tmp_root or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(palace_path).expanduser().resolve().parent / ".process-tmp").resolve()


def process_temp_dir(
    settings: MemorySettings,
    palace_path: Path,
    memory_space_id: str,
) -> Path:
    """Return and create the private temp directory for one Realm owner."""

    memory_space_id = validate_memory_space_id(memory_space_id)
    temp_dir = resolve_process_temp_root(settings, palace_path) / memory_space_storage_name(
        memory_space_id
    )
    temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        temp_dir.chmod(0o700)
    except OSError:
        # Some mounted filesystems do not expose POSIX mode bits. Location
        # safety is checked separately by agent_runner.
        pass
    return temp_dir


def process_temp_subprocess_env(
    settings: MemorySettings,
    palace_path: Path,
    memory_space_id: str,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an Agent environment with temp isolation active before import.

    Chroma's native components may inspect SQLite/temp settings while Python
    imports their extension modules. Setting these variables only inside the
    Agent's ``main()`` is therefore too late; the parent must pass them to
    ``Popen`` as well.
    """

    temp_dir = process_temp_dir(settings, palace_path, memory_space_id)
    env = dict(os.environ if base_env is None else base_env)
    value = str(temp_dir)
    env["TMPDIR"] = value
    env["SQLITE_TMPDIR"] = value
    env["EIDOLON_MEMORY_PROCESS_TMP_ROOT"] = str(temp_dir.parent)
    return env


def configure_process_temp(
    settings: MemorySettings,
    palace_path: Path,
    memory_space_id: str,
) -> Path:
    """Create and activate a private temp directory for one memory space.

    Kept for callers that genuinely serve exactly one space and want its temp
    files under their own name. Prefer :func:`configure_process_temp_root` in a
    process that may serve several: ``TMPDIR`` is a process-wide setting, so
    per-space directories cannot all be active, and whichever was configured last
    would silently take the others' temp files.
    """

    memory_space_id = validate_memory_space_id(memory_space_id)
    temp_dir = process_temp_dir(settings, palace_path, memory_space_id)
    _activate(temp_dir, memory_space_id=memory_space_id)
    return temp_dir


def configure_process_temp_root(settings: MemorySettings, palaces_root: Path) -> Path:
    """Point this process's temp files at a directory we control.

    What this is for is keeping SQLite's spill files and Chroma's scratch off the
    host temp directory — which may be synced by iCloud or Dropbox, cleaned under
    us mid-write, or on a filesystem that does not honour the locking these stores
    assume.

    It is process-scoped rather than space-scoped because ``TMPDIR`` is. Temp
    files are short-lived and randomly named, so sharing one directory between
    the spaces a process serves costs nothing; what mattered was never being in
    the host's temp directory.

    Must run before any MemPalace or Chroma client is opened. Those stacks read
    these variables while their native extensions load, so setting them later has
    no effect on the handles already built.
    """

    temp_dir = resolve_process_temp_root(settings, palaces_root / "_")
    temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        temp_dir.chmod(0o700)
    except OSError:
        # Some mounted filesystems do not expose POSIX mode bits. Location
        # safety is checked separately when a palace is opened.
        pass
    _activate(temp_dir)
    return temp_dir


def _activate(temp_dir: Path, *, memory_space_id: str | None = None) -> None:
    value = str(temp_dir)
    # Both names are set because the Python and Rust SQLite stacks do not always
    # consult the same one on every platform.
    os.environ["TMPDIR"] = value
    os.environ["SQLITE_TMPDIR"] = value
    # ``tempfile`` caches its decision on first use. Reset it in case an earlier
    # import already read the host TMPDIR.
    tempfile.tempdir = None
    log.info(
        "process_temp_configured",
        memory_space_id=memory_space_id,
        path=value,
    )
