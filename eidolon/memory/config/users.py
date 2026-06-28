"""Load and validate admin-owned memory realm routing for memory runtime.

Runtime source of truth is eidolon_admin's owner workspace data. Memory only
consumes active owners, active companions, and active memory realms; it does
not derive routes from tenant/user identifiers.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin

from eidolon_sdk.memory import stable_memory_realm_port
from pydantic import BaseModel, Field, field_validator, model_validator

from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import validate_memory_space_id


class UsersSourceUnavailable(RuntimeError):
    """Admin owner workspace data could not be read.

    Supervisor treats this as "do not change the current runtime set" instead
    of an empty realm list, so an admin/API blip does not stop every worker.
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


def _load_json(url: str, *, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return raw if isinstance(raw, dict) else {}


def _stable_realm_port(realm_id: str, *, base_port: int, used_ports: set[int]) -> int:
    return stable_memory_realm_port(
        realm_id,
        base_port=base_port,
        used_ports=used_ports,
    )


def _entry_from_memory_realm(
    realm: dict,
    *,
    owner: dict,
    companion: dict,
    port: int,
) -> UserEntry | None:
    realm_id = str(realm.get("realm_id") or "").strip()
    owner_id = str(realm.get("owner_id") or owner.get("owner_id") or "").strip()
    companion_id = str(realm.get("companion_id") or companion.get("companion_id") or "").strip()
    if not realm_id or not owner_id or not companion_id:
        return None
    config = realm.get("engine_config_json") or {}
    return UserEntry(
        id=realm_id,
        owner_id=owner_id,
        companion_id=companion_id,
        port=port,
        enabled=(
            str(owner.get("status") or "").lower() == "active"
            and str(companion.get("status") or "").lower() == "active"
            and str(realm.get("status") or "").lower() == "active"
        ),
        consolidator=_consolidator_from_admin(config.get("consolidator") or {}),
    )


def _load_memory_realms_from_admin_api(settings: MemorySettings | None = None) -> UsersConfig:
    cfg = settings or get_memory_settings()
    base_url = resolve_admin_api_url(cfg)
    timeout = cfg.supervisor.admin_api_timeout_seconds
    owners_url = urljoin(base_url.rstrip("/") + "/", "api/owners")
    try:
        owners_payload = _load_json(owners_url, timeout=timeout)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise UsersSourceUnavailable(
            f"admin owner registry unavailable at {owners_url}: {exc}"
        ) from exc

    entries: list[UserEntry] = []
    used_ports: set[int] = set()
    owners = sorted(
        [owner for owner in owners_payload.get("owners", []) or [] if isinstance(owner, dict)],
        key=lambda item: str(item.get("owner_id") or ""),
    )
    for owner in owners:
        if str(owner.get("status") or "").lower() != "active":
            continue
        owner_id = str(owner.get("owner_id") or "").strip()
        if not owner_id:
            continue
        quoted_owner_id = quote(owner_id, safe="")
        companions_url = urljoin(
            base_url.rstrip("/") + "/",
            f"api/owners/{quoted_owner_id}/companions",
        )
        realms_url = urljoin(
            base_url.rstrip("/") + "/",
            f"api/owners/{quoted_owner_id}/memory-realms",
        )
        try:
            companions_payload = _load_json(companions_url, timeout=timeout)
            realms_payload = _load_json(realms_url, timeout=timeout)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise UsersSourceUnavailable(
                f"admin owner workspace unavailable for {owner_id!r}: {exc}"
            ) from exc

        companions = {
            str(companion.get("companion_id") or ""): companion
            for companion in companions_payload.get("companions", []) or []
            if isinstance(companion, dict)
        }
        realms = sorted(
            [
                realm
                for realm in realms_payload.get("memory_realms", []) or []
                if isinstance(realm, dict)
            ],
            key=lambda item: str(item.get("realm_id") or ""),
        )
        for realm in realms:
            companion_id = str(realm.get("companion_id") or "").strip()
            companion = companions.get(companion_id)
            if companion is None:
                continue
            port = _stable_realm_port(
                str(realm.get("realm_id") or ""),
                base_port=cfg.mcp_http.port,
                used_ports=used_ports,
            )
            used_ports.add(port)
            entry = _entry_from_memory_realm(
                realm,
                owner=owner,
                companion=companion,
                port=port,
            )
            if entry is not None:
                entries.append(entry)
    return UsersConfig(users=entries)


def load_users_config(settings: MemorySettings | None = None) -> UsersConfig:
    """Read and validate memory realms from eidolon_admin's owner APIs."""
    return _load_memory_realms_from_admin_api(settings)
