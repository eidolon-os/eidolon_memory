"""Load and validate the admin-owned user registry for memory runtime.

Runtime source of truth is eidolon_admin's ``GET /api/users``. Memory only
consumes that registry and reconciles workers to the project-wide
``spec.enabled`` flag.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urljoin

from pydantic import BaseModel, Field, field_validator, model_validator

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import validate_memory_space_id


class UsersSourceUnavailable(RuntimeError):
    """Admin user registry could not be read.

    Supervisor treats this as "do not change the current runtime set" instead
    of an empty user list, so an admin/API blip does not stop every worker.
    """


class ConsolidatorUserConfig(BaseModel):
    """Phase 4 — per-user knobs for the background theme worker.

    Opt-in by default (``enabled=False``) so adding a new registry user does
    not silently start an extra LLM-consuming daemon.
    """

    enabled: bool = False
    interval_hours: float = Field(gt=0, default=6.0)
    window_days: int = Field(gt=0, default=30)
    min_drawers: int = Field(ge=1, default=3)
    min_confidence: float = Field(ge=0.0, le=1.0, default=0.6)


class UserEntry(BaseModel):
    id: str
    owner_id: str | None = None
    companion_id: str | None = None
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    palace_path: str = ""  # absolute override; empty = use default per-user palace
    # Phase 4 — optional. ``None`` (the default) means "no consolidator for
    # this user"; explicit ``ConsolidatorUserConfig`` is the opt-in marker.
    consolidator: ConsolidatorUserConfig | None = None

    @field_validator("id")
    @classmethod
    def _id_valid(cls, value: str) -> str:
        return validate_memory_space_id(value)

    def consolidator_enabled(self) -> bool:
        """Convenience: True iff a consolidator block exists AND is enabled."""
        return bool(self.consolidator and self.consolidator.enabled)


class UsersConfig(BaseModel):
    users: list[UserEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dup_id_or_port(self) -> UsersConfig:
        seen_ids: set[str] = set()
        seen_ports: dict[int, str] = {}
        for u in self.users:
            if u.id in seen_ids:
                msg = f"user registry: duplicate user id {u.id!r}"
                raise ValueError(msg)
            seen_ids.add(u.id)
            if u.enabled:
                if u.port in seen_ports:
                    msg = (
                        f"user registry: port {u.port} collision between "
                        f"{seen_ports[u.port]!r} and {u.id!r} (both enabled)"
                    )
                    raise ValueError(msg)
                seen_ports[u.port] = u.id
        return self

    def enabled_users(self) -> list[UserEntry]:
        return [u for u in self.users if u.enabled]

    def find(self, user_id: str) -> UserEntry | None:
        return next((u for u in self.users if u.id == user_id), None)


def resolve_admin_api_url(settings: MemorySettings | None = None) -> str:
    env_url = os.environ.get("EIDOLON_ADMIN_API_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    cfg = settings or get_memory_settings()
    configured = (cfg.supervisor.admin_api_url or "").strip()
    if configured:
        return configured.rstrip("/")

    host = os.environ.get("EIDOLON_ADMIN_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("EIDOLON_ADMIN_API_PORT", "9000").strip() or "9000"
    return f"http://{host}:{port}"

def _consolidator_from_admin(raw: dict) -> ConsolidatorUserConfig | None:
    if not isinstance(raw, dict):
        return None
    return ConsolidatorUserConfig(
        enabled=bool(raw.get("enabled", False)),
        interval_hours=float(raw.get("interval_hours", 6.0) or 6.0),
        window_days=int(raw.get("window_days", 30) or 30),
        min_drawers=int(raw.get("min_drawers", 3) or 3),
        min_confidence=float(raw.get("min_confidence", 0.6) or 0.6),
    )


def _entry_from_admin_view(view: dict) -> UserEntry | None:
    spec = view.get("spec") if isinstance(view, dict) and "spec" in view else view
    if not isinstance(spec, dict):
        return None
    memory_space_id = str(
        spec.get("memory_realm_id")
        or spec.get("memory_space_id")
        or spec.get("user_id")
        or ""
    ).strip()
    port = int(spec.get("memory_port", 0) or 0)
    if port <= 0:
        raw_url = str(view.get("mcp_http_url") or "") if isinstance(view, dict) else ""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(raw_url)
            port = parsed.port or 0
        except Exception:  # noqa: BLE001
            port = 0
    if not memory_space_id or port <= 0:
        return None
    return UserEntry(
        id=memory_space_id,
        owner_id=str(spec.get("owner_id") or "").strip() or None,
        companion_id=str(spec.get("companion_id") or "").strip() or None,
        port=port,
        enabled=bool(spec.get("enabled", True)),
        palace_path=str(spec.get("palace_path") or ""),
        consolidator=_consolidator_from_admin(spec.get("consolidator") or {}),
    )


def _load_users_from_admin_api(settings: MemorySettings | None = None) -> UsersConfig:
    cfg = settings or get_memory_settings()
    base_url = resolve_admin_api_url(cfg)
    timeout = cfg.supervisor.admin_api_timeout_seconds
    url = urljoin(base_url.rstrip("/") + "/", "api/users/registry")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise UsersSourceUnavailable(
            f"admin user registry unavailable at {url}: {exc}"
        ) from exc

    entries = []
    for view in raw.get("users", []) or []:
        if not isinstance(view, dict):
            continue
        entry = _entry_from_admin_view(view)
        if entry is not None:
            entries.append(entry)
    return UsersConfig(users=entries)


def load_users_config(settings: MemorySettings | None = None) -> UsersConfig:
    """Read and validate users from eidolon_admin's registry API."""
    return _load_users_from_admin_api(settings)
