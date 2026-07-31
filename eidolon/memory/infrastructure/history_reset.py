"""Guarded destructive reset of Memory history while preserving Realm identity."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO

from eidolon_memory_contracts import memory_space_storage_name

from eidolon.memory.infrastructure.nats.names import nats_safe_name

CONFIRMATION = "DELETE_ALL_MEMORY_HISTORY_KEEP_REALMS"
_REPAIR_ARCHIVE_RE = re.compile(r"^(?P<storage>.+)\.pre-rebuild-\d{8}-\d{6}$")


class HistoryResetSafetyError(RuntimeError):
    """Raised before deletion when reset scope or owner state is unsafe."""


def active_realm_ids(registry_db: Path) -> list[str]:
    """Read active Realm IDs without modifying the registry database."""

    registry_db = Path(registry_db).expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{registry_db}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        rows = connection.execute(
            "SELECT realm_id FROM memory_realms WHERE status = 'active' ORDER BY realm_id"
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def realm_registry_digest(registry_db: Path) -> str:
    """Hash only Realm identity rows, ignoring unrelated registry writes."""

    registry_db = Path(registry_db).expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{registry_db}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        rows = connection.execute(
            "SELECT realm_id, owner_id, companion_id, status "
            "FROM memory_realms ORDER BY realm_id"
        ).fetchall()
    finally:
        connection.close()
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_reset_scope(palaces_root: Path, realm_ids: list[str]) -> dict[str, Path]:
    """Require a one-to-one mapping between active Realms and Palace dirs."""

    if not realm_ids:
        raise HistoryResetSafetyError("registry contains no active memory Realms")
    palaces_root = Path(palaces_root).expanduser().resolve()
    expected = {
        realm_id: palaces_root / memory_space_storage_name(realm_id) for realm_id in realm_ids
    }
    missing = [realm_id for realm_id, path in expected.items() if not path.is_dir()]
    actual_names = {
        path.name
        for path in palaces_root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }
    expected_names = {path.name for path in expected.values()}
    unexpected = sorted(
        name
        for name in actual_names - expected_names
        if not (
            (match := _REPAIR_ARCHIVE_RE.fullmatch(name))
            and match.group("storage") in expected_names
        )
    )
    if missing or unexpected:
        raise HistoryResetSafetyError(
            f"Palace/Realm scope mismatch: missing={missing!r} unexpected={unexpected!r}"
        )
    return expected


def clear_repair_archives(palaces_root: Path, palace_paths: list[Path]) -> list[str]:
    """Remove only MemPalace archives belonging to the preserved Realms."""

    palaces_root = Path(palaces_root).expanduser().resolve()
    expected_names = {Path(path).name for path in palace_paths}
    removed: list[str] = []
    for path in sorted(palaces_root.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        match = _REPAIR_ARCHIVE_RE.fullmatch(path.name)
        if match and match.group("storage") in expected_names:
            shutil.rmtree(path)
            removed.append(str(path))
    return removed


@contextmanager
def acquire_realm_reset_locks(run_dir: Path, realm_ids: list[str]) -> Iterator[None]:
    """Prove every Realm owner is stopped and hold its lock through reset."""

    run_dir = Path(run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        handles: list[BinaryIO] = []
        for realm_id in realm_ids:
            path = run_dir / f"eidolon-memory-agent-{nats_safe_name(realm_id)}.lock"
            handle = stack.enter_context(path.open("a+b"))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HistoryResetSafetyError(f"Realm owner is still running: {realm_id}") from exc
            handles.append(handle)
        try:
            yield
        finally:
            for handle in handles:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def clear_palace_contents(palace_path: Path) -> int:
    """Remove every artifact inside a Palace but keep the Realm directory."""

    palace_path = Path(palace_path).expanduser().resolve()
    if not palace_path.is_dir():
        raise HistoryResetSafetyError(f"Palace directory is missing: {palace_path}")
    removed = 0
    for child in list(palace_path.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            raise HistoryResetSafetyError(f"unsupported Palace entry: {child}")
        removed += 1
    return removed


def clear_realm_process_temp(process_tmp_root: Path, realm_id: str) -> bool:
    path = Path(process_tmp_root).expanduser().resolve() / memory_space_storage_name(realm_id)
    if not path.exists():
        return False
    if not path.is_dir() or path.is_symlink():
        raise HistoryResetSafetyError(f"unsafe process temp path: {path}")
    shutil.rmtree(path)
    return True


def truncate_memory_history_files(log_dir: Path, dlq_path: Path) -> list[str]:
    """Truncate memory-owned logs/DLQ that may contain recalled user text."""

    targets = set(Path(log_dir).expanduser().resolve().glob("*.log"))
    targets.add(Path(dlq_path).expanduser().resolve())
    changed: list[str] = []
    for path in sorted(targets):
        if path.is_file():
            path.write_bytes(b"")
            changed.append(str(path))
    return changed
