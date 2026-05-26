"""Load and validate ``users.yaml`` for the supervisor.

Resolution order for the users.yaml path (highest priority first):

1. Caller-supplied ``path`` argument
2. ``EIDOLON_MEMORY_USERS_YAML`` environment variable
3. ``settings.supervisor.users_file`` (absolute, or relative to repo root)
4. ``config/users.yaml`` (repo root)

Schema:

.. code-block:: yaml

    users:
      - id: alice
        port: 8030
        enabled: true
        palace_path: ""    # optional override; default ~/eidolon/palaces/<id>/
        consolidator:      # optional; Phase 4 background theme worker
          enabled: false
          interval_hours: 6
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
_CONFIG_DIR = _REPO_ROOT / "config"
_DEFAULT_USERS_PATH = _CONFIG_DIR / "users.yaml"
_USERS_TEMPLATE_PATH = _CONFIG_DIR / "users.yaml.tpl"


class ConsolidatorUserConfig(BaseModel):
    """Phase 4 — per-user knobs for the background theme worker.

    Opt-in by default (``enabled=False``) — adding a new user to ``users.yaml``
    should not silently start an extra LLM-consuming daemon. Set
    ``enabled: true`` per user to spawn ``eidolon-memory-consolidator``
    alongside that user's ``agent_runner`` from within
    ``eidolon-memory-supervisor``.
    """

    enabled: bool = False
    interval_hours: float = Field(gt=0, default=6.0)
    window_days: int = Field(gt=0, default=30)
    min_drawers: int = Field(ge=1, default=3)
    min_confidence: float = Field(ge=0.0, le=1.0, default=0.6)


class UserEntry(BaseModel):
    id: str
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    palace_path: str = ""  # absolute override; empty = use default per-user palace
    # Phase 4 — optional. ``None`` (the default) means "no consolidator for
    # this user"; explicit ``ConsolidatorUserConfig`` is the opt-in marker.
    consolidator: ConsolidatorUserConfig | None = None

    @field_validator("id")
    @classmethod
    def _id_valid(cls, value: str) -> str:
        return validate_user_id(value)

    def consolidator_enabled(self) -> bool:
        """Convenience: True iff a consolidator block exists AND is enabled."""
        return bool(self.consolidator and self.consolidator.enabled)


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
    """Path to ``config/users.yaml.tpl`` (single ``default`` user seed)."""
    return _USERS_TEMPLATE_PATH


def ensure_users_yaml_exists(users_path: Path) -> bool:
    """If ``users_path`` is missing, seed it from ``config/users.yaml.tpl``.

    Returns ``True`` when the file was created, ``False`` when an existing
    file is kept untouched. Raises :class:`FileNotFoundError` when the template
    is missing (copy ``config/users.yaml.tpl`` to ``config/users.yaml``).
    """
    users_path = Path(users_path)
    if users_path.is_file():
        return False
    if not _USERS_TEMPLATE_PATH.is_file():
        msg = f"users.yaml.tpl missing at {_USERS_TEMPLATE_PATH}"
        raise FileNotFoundError(msg)
    users_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_USERS_TEMPLATE_PATH, users_path)
    log.info("users_yaml_seeded_from_template", path=str(users_path))
    return True
