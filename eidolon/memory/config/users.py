"""The roster of memory spaces this deployment serves.

Holds the shapes (:class:`UserEntry`, :class:`UsersConfig`) plus the Eidolon OS
source for them: the System Data authority's versioned Memory runtime roster.
The service consumes one bounded projection of active Owner, Companion and
Realm facts and never opens System Data storage itself.

Deployments outside the OS get their roster from
:mod:`eidolon.memory.config.registry_static` instead. Callers reach either one
through :func:`eidolon.memory.config.registry.load_users_config`, which picks a
source; this module stays a leaf so both sources can depend on it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from eidolon_memory_contracts import stable_memory_realm_port
from pydantic import BaseModel, Field, field_validator, model_validator

from eidolon.memory.config.memory_settings import (
    MemorySettings,
    get_memory_settings,
)
from eidolon.memory.config.palace_directory import validate_memory_space_id


class RegistrySourceUnavailable(RuntimeError):
    """The roster could not be read.

    Distinct from "the roster is empty", and the distinction matters: an empty
    roster means stop serving everything, while an unreadable source means keep
    serving what we already have. The supervisor treats this exception as the
    latter, so an admin restart or a momentarily unreadable file does not tear
    down every running worker.
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
                msg = f"memory realm registry: duplicate realm id {u.id!r}"
                raise ValueError(msg)
            seen_ids.add(u.id)
            if u.enabled:
                if u.port in seen_ports:
                    msg = (
                        f"memory realm registry: port {u.port} collision between "
                        f"{seen_ports[u.port]!r} and {u.id!r} (both enabled)"
                    )
                    raise ValueError(msg)
                seen_ports[u.port] = u.id
        return self

    def enabled_users(self) -> list[UserEntry]:
        return [u for u in self.users if u.enabled]

    def find(self, user_id: str) -> UserEntry | None:
        return next((u for u in self.users if u.id == user_id), None)


def resolve_system_data_roster_url(settings: MemorySettings | None = None) -> str:
    cfg = settings or get_memory_settings()
    override = os.environ.get("EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_URL", "").strip()
    base_url = override or urljoin(
        cfg.registry.system_data_url.rstrip("/") + "/",
        "api/companion-authority/v1/memory-runtime-roster",
    )
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RegistrySourceUnavailable("System Data Memory roster URL is invalid")
    return base_url


def _consolidator_from_engine_config(raw: dict) -> ConsolidatorUserConfig | None:
    if not isinstance(raw, dict):
        return None
    return ConsolidatorUserConfig(
        enabled=bool(raw.get("enabled", False)),
        interval_hours=float(raw.get("interval_hours", 6.0) or 6.0),
        window_days=int(raw.get("window_days", 30) or 30),
        min_drawers=int(raw.get("min_drawers", 3) or 3),
        min_confidence=float(raw.get("min_confidence", 0.6) or 0.6),
    )


def _load_json(url: str, *, timeout: float, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return raw if isinstance(raw, dict) else {}


def stable_realm_port(realm_id: str, *, base_port: int, used_ports: set[int]) -> int:
    """Derive a stable MCP port for a space, avoiding ones already taken.

    Deterministic in the space id, so a space keeps its port across restarts
    without anyone recording the allocation.
    """

    return stable_memory_realm_port(
        realm_id,
        base_port=base_port,
        used_ports=used_ports,
    )


def _entry_from_memory_realm(
    realm: dict,
    *,
    port: int,
) -> UserEntry:
    realm_id = str(realm.get("realm_id") or "").strip()
    owner_id = str(realm.get("owner_id") or "").strip()
    companion_id = str(realm.get("companion_id") or "").strip()
    if not realm_id or not owner_id or not companion_id:
        raise RegistrySourceUnavailable("System Data Memory roster entry is incomplete")
    config = realm.get("engine_config") or {}
    if not isinstance(config, dict):
        raise RegistrySourceUnavailable("System Data Memory roster engine_config is invalid")
    return UserEntry(
        id=realm_id,
        owner_id=owner_id,
        companion_id=companion_id,
        port=port,
        enabled=True,
        consolidator=_consolidator_from_engine_config(config.get("consolidator") or {}),
    )


class SystemDataRegistry:
    """Roster from the versioned System Data Memory authority contract."""

    def __init__(self, settings: MemorySettings) -> None:
        self._settings = settings

    def load(self) -> UsersConfig:
        return _load_memory_realms_from_system_data(self._settings)


def _load_memory_realms_from_system_data(
    settings: MemorySettings | None = None,
) -> UsersConfig:
    cfg = settings or get_memory_settings()
    url = resolve_system_data_roster_url(cfg)
    token_env = cfg.registry.system_data_token_env.strip()
    token = os.environ.get(token_env, "").strip() if token_env else ""
    if len(token) < 24:
        raise RegistrySourceUnavailable(
            "System Data Memory roster service credential is unavailable"
        )
    try:
        payload = _load_json(
            url,
            timeout=cfg.registry.request_timeout_seconds,
            token=token,
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RegistrySourceUnavailable(
            f"System Data Memory roster unavailable at {url}: {exc}"
        ) from exc
    if payload.get("contract_version") != "1" or payload.get("operation") != (
        "memory.runtime-roster"
    ):
        raise RegistrySourceUnavailable("System Data Memory roster contract identity is invalid")
    raw_realms = payload.get("realms")
    if not isinstance(raw_realms, list) or any(not isinstance(item, dict) for item in raw_realms):
        raise RegistrySourceUnavailable("System Data Memory roster payload is invalid")

    entries: list[UserEntry] = []
    used_ports: set[int] = set()
    for realm in sorted(raw_realms, key=lambda item: str(item.get("realm_id") or "")):
        port = stable_realm_port(
            str(realm.get("realm_id") or ""),
            base_port=cfg.mcp_http.port,
            used_ports=used_ports,
        )
        used_ports.add(port)
        entries.append(_entry_from_memory_realm(realm, port=port))
    return UsersConfig(users=entries)
