"""Load and validate ``users.yaml`` for the supervisor.

Resolution order for the users.yaml path (highest priority first):

1. Caller-supplied ``path`` argument
2. ``EIDOLON_MEMORY_USERS_YAML`` environment variable
3. ``settings.supervisor.users_file`` (absolute, or relative to repo root)
4. ``~/.eidolon/users.yaml``

Schema:

.. code-block:: yaml

    users:
      - id: alice
        port: 8030
        enabled: true
        palace_path: ""    # optional override; default ~/eidolon/palaces/<id>/
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import validate_user_id
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_USERS_PATH = _REPO_ROOT / "config" / "users.yaml"
_BUNDLED_USERS_TPL = Path(__file__).resolve().parent / "users.yaml.tpl"


class UserEntry(BaseModel):
    id: str
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    palace_path: str = ""  # absolute override; empty = use default per-user palace

    @field_validator("id")
    @classmethod
    def _id_valid(cls, value: str) -> str:
        return validate_user_id(value)


class UsersConfig(BaseModel):
    users: list[UserEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dup_id_or_port(self) -> "UsersConfig":
        seen_ids: set[str] = set()
        seen_ports: dict[int, str] = {}
        for u in self.users:
            if u.id in seen_ids:
                msg = f"users.yaml: duplicate user id {u.id!r}"
                raise ValueError(msg)
            seen_ids.add(u.id)
            if u.enabled:
                if u.port in seen_ports:
                    msg = (
                        f"users.yaml: port {u.port} collision between "
                        f"{seen_ports[u.port]!r} and {u.id!r} (both enabled)"
                    )
                    raise ValueError(msg)
                seen_ports[u.port] = u.id
        return self

    def enabled_users(self) -> list[UserEntry]:
        return [u for u in self.users if u.enabled]

    def find(self, user_id: str) -> UserEntry | None:
        return next((u for u in self.users if u.id == user_id), None)


def resolve_users_file_path(
    settings: MemorySettings | None = None,
    *,
    path: str | Path | None = None,
) -> Path:
    """Apply the priority order documented in the module docstring."""
    if path:
        return Path(path).expanduser().resolve()

    env = os.environ.get("EIDOLON_MEMORY_USERS_YAML", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    cfg = settings or get_memory_settings()
    configured = (cfg.supervisor.users_file or "").strip()
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = _REPO_ROOT / p
        return p.resolve()

    return _DEFAULT_USERS_PATH.resolve()


def load_users_config(
    settings: MemorySettings | None = None,
    *,
    path: str | Path | None = None,
) -> UsersConfig:
    """Read + validate ``users.yaml``. Missing file → empty config (no users)."""
    resolved = resolve_users_file_path(settings, path=path)
    if not resolved.is_file():
        log.warning("users_yaml_missing", path=str(resolved))
        return UsersConfig(users=[])
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    return UsersConfig.model_validate(raw)


def bundled_users_template_path() -> Path:
    """Path to the package-bundled ``users.yaml.tpl`` (single ``default`` user)."""
    return _BUNDLED_USERS_TPL


def ensure_users_yaml_exists(users_path: Path) -> bool:
    """If ``users_path`` is missing, seed it by copying the bundled ``.tpl``.

    Returns ``True`` when the file was created, ``False`` when an existing
    file is kept untouched. Raises :class:`FileNotFoundError` when the bundled
    template is unexpectedly absent (would indicate a broken install).
    """
    users_path = Path(users_path)
    if users_path.is_file():
        return False
    if not _BUNDLED_USERS_TPL.is_file():
        msg = f"bundled users.yaml.tpl missing at {_BUNDLED_USERS_TPL}"
        raise FileNotFoundError(msg)
    users_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_BUNDLED_USERS_TPL, users_path)
    log.info("users_yaml_seeded_from_template", path=str(users_path))
    return True
