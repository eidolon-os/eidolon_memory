"""Atomic, cross-process-safe writes to ``users.yaml``.

Why this lives in core:
- The supervisor's SIGHUP path needs to compare a fresh read against running
  children. If two writers (e.g. supervisor's seed-from-tpl + an ops CLI
  + a human ``vim``) race, the file can end up with malformed yaml or
  schema-invalid content.
- The atomic rename (tmp + ``os.replace``) prevents partial-write corruption
  *within* one writer, but does NOT serialize multiple writers.
- We add a POSIX advisory lock (``fcntl.flock``) on a sibling ``.lock`` file
  so writers serialize voluntarily. Readers do NOT take the lock — atomic
  rename guarantees they see either the old or new file completely.

Schema validation goes through :class:`UsersConfig` so duplicate ids /
port collisions are caught before persisting.

API surface:
- :func:`upsert_user`    add or replace by id (returns new config)
- :func:`update_enabled` flip enabled flag (raises if id absent)
- :func:`remove_user`    delete a user by id (raises if absent)
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from eidolon.memory.config.users import UserEntry, UsersConfig, load_users_config
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class UsersYamlError(ValueError):
    """Raised on validation failures (duplicate id/port, schema break, missing user)."""


@contextlib.contextmanager
def _writer_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    """fcntl exclusive lock on ``<path>.lock`` for cross-process write serialization.

    Times out by polling because POSIX ``flock`` blocks indefinitely
    otherwise; we'd rather raise than deadlock a long-running supervisor.
    """
    import time

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with lock_path.open("a+") as fp:
        while True:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    msg = (
                        f"users.yaml lock held longer than {timeout_seconds}s "
                        f"({lock_path}); another writer hung?"
                    )
                    raise UsersYamlError(msg) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _atomic_write_yaml(path: Path, cfg: UsersConfig) -> None:
    """tmp-file + ``os.replace`` for partial-write safety on a single writer."""
    payload: dict[str, Any] = {
        "users": [u.model_dump(mode="json") for u in cfg.users],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".users.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            yaml.safe_dump(payload, tmp, allow_unicode=True, sort_keys=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""  # successfully consumed
    finally:
        if tmp_path and Path(tmp_path).exists():
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def upsert_user(path: Path, entry: UserEntry) -> UsersConfig:
    """Add ``entry`` to ``users.yaml`` (or replace by id). Returns the new
    config. Raises :class:`UsersYamlError` if it would create a duplicate
    id or port collision against another *enabled* user.
    """
    with _writer_lock(path):
        current = load_users_config(path=path)
        users = [u for u in current.users if u.id != entry.id]
        users.append(entry)
        try:
            new_cfg = UsersConfig(users=users)
        except ValueError as exc:
            raise UsersYamlError(str(exc)) from exc
        _atomic_write_yaml(path, new_cfg)
        log.info(
            "users_yaml_upsert", user_id=entry.id, port=entry.port, enabled=entry.enabled
        )
        return new_cfg


def update_enabled(path: Path, user_id: str, enabled: bool) -> UsersConfig:
    """Flip the ``enabled`` flag on an existing user. Raises if absent."""
    with _writer_lock(path):
        current = load_users_config(path=path)
        new_users: list[UserEntry] = []
        found = False
        for u in current.users:
            if u.id == user_id:
                found = True
                new_users.append(u.model_copy(update={"enabled": enabled}))
            else:
                new_users.append(u)
        if not found:
            raise UsersYamlError(f"user {user_id!r} not found in users.yaml")
        try:
            new_cfg = UsersConfig(users=new_users)
        except ValueError as exc:
            raise UsersYamlError(str(exc)) from exc
        _atomic_write_yaml(path, new_cfg)
        log.info("users_yaml_enable_changed", user_id=user_id, enabled=enabled)
        return new_cfg


def remove_user(path: Path, user_id: str) -> UsersConfig:
    """Delete a user by id (palace data on disk is NOT touched). Raises if absent."""
    with _writer_lock(path):
        current = load_users_config(path=path)
        new_users = [u for u in current.users if u.id != user_id]
        if len(new_users) == len(current.users):
            raise UsersYamlError(f"user {user_id!r} not found in users.yaml")
        new_cfg = UsersConfig(users=new_users)
        _atomic_write_yaml(path, new_cfg)
        log.info("users_yaml_removed", user_id=user_id)
        return new_cfg
