"""Per-Realm temporary-directory isolation for embedded storage processes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from eidolon_sdk.memory import memory_space_storage_name, validate_memory_space_id

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
    """Create and activate a private temp directory for one Realm owner.

    This must run after the Realm process lock is acquired and before a
    MemPalace/Chroma client is opened.  Both generic and SQLite-specific env
    variables are set because the Python and Rust SQLite stacks do not always
    consult the same variable on every platform.
    """

    memory_space_id = validate_memory_space_id(memory_space_id)
    temp_dir = process_temp_dir(settings, palace_path, memory_space_id)

    value = str(temp_dir)
    os.environ["TMPDIR"] = value
    os.environ["SQLITE_TMPDIR"] = value
    # ``tempfile`` caches its decision. Reset it in case an early import used
    # the host TMPDIR before the agent configured Realm isolation.
    tempfile.tempdir = None
    log.info(
        "agent_runner_process_temp_configured",
        memory_space_id=memory_space_id,
        path=value,
    )
    return temp_dir
