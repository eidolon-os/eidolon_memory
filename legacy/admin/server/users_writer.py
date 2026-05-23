"""Mutate ``users.yaml`` from the admin process.

Writes are atomic (tmp + ``os.replace``) so concurrent readers
(supervisor, admin probes) never see a partial file. The new config is
re-validated through :class:`UsersConfig` before persisting — invariant:
no duplicate ids, no port collisions.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from eidolon.memory.config.users import UserEntry, UsersConfig, load_users_config

from eidolon.memory.config.memory_settings import MemorySettings


class UsersYamlError(ValueError):
    """Raised on validation failures (duplicate id/port, schema break)."""


def upsert_user(
    settings: MemorySettings,
    path: Path,
    entry: UserEntry,
) -> UsersConfig:
    """Add ``entry`` to ``users.yaml`` (or replace by id). Returns new config."""
    current = load_users_config(settings, path=path)
    users = [u for u in current.users if u.id != entry.id]
    users.append(entry)
    new_cfg = UsersConfig(users=users)  # re-validates port + id uniqueness
    _atomic_write(path, new_cfg)
    return new_cfg


def update_enabled(
    settings: MemorySettings,
    path: Path,
    user_id: str,
    enabled: bool,
) -> UsersConfig:
    current = load_users_config(settings, path=path)
    found = False
    new_users: list[UserEntry] = []
    for u in current.users:
        if u.id == user_id:
            found = True
            new_users.append(u.model_copy(update={"enabled": enabled}))
        else:
            new_users.append(u)
    if not found:
        msg = f"user {user_id!r} not found in users.yaml"
        raise UsersYamlError(msg)
    new_cfg = UsersConfig(users=new_users)
    _atomic_write(path, new_cfg)
    return new_cfg


def _atomic_write(path: Path, cfg: UsersConfig) -> None:
    data: dict[str, Any] = {
        "users": [u.model_dump(mode="json") for u in cfg.users],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(path.parent),
        prefix=".users.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        yaml.safe_dump(data, tmp, allow_unicode=True, sort_keys=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)
