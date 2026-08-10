"""Where the roster of memory spaces to serve comes from.

The service does not decide which spaces exist — it serves the ones it is told
about. Who does the telling differs by deployment: inside Eidolon OS the System
Data authority publishes a versioned runtime roster, while a standalone
deployment has an operator and a file.

Both answer the same question, so both go behind one port and callers do not
branch on deployment shape. This module knows about the sources; the sources do
not know about it, which keeps the dependency one-way and lets both of them
depend on the roster shapes in :mod:`eidolon.memory.config.users`.

``RegistrySourceUnavailable`` lives next to those shapes, since both
implementations raise it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from eidolon.memory.config.memory_settings import (
    MemorySettings,
    default_memory_settings_path,
    get_memory_settings,
)
from eidolon.memory.config.registry_static import StaticFileRegistry
from eidolon.memory.config.users import (
    RegistrySourceUnavailable,
    SystemDataRegistry,
    UsersConfig,
)


@runtime_checkable
class RegistryPort(Protocol):
    """Reads the current roster of memory spaces."""

    def load(self) -> UsersConfig:
        """Return the spaces to serve, or raise RegistrySourceUnavailable."""
        ...


def resolve_static_registry_path(settings: MemorySettings) -> Path:
    """Locate the static roster, resolving a relative path next to settings.yaml."""

    declared = (settings.registry.static_path or "").strip()
    if not declared:
        raise RegistrySourceUnavailable(
            "registry.source is 'static' but registry.static_path is empty"
        )
    path = Path(declared).expanduser()
    if path.is_absolute():
        return path
    return (default_memory_settings_path().parent / path).resolve()


def build_registry(settings: MemorySettings | None = None) -> RegistryPort:
    """Pick the roster source this deployment is configured for."""

    cfg = settings or get_memory_settings()
    if cfg.registry.source == "static":
        return StaticFileRegistry(cfg, path=resolve_static_registry_path(cfg))
    return SystemDataRegistry(cfg)


def load_users_config(settings: MemorySettings | None = None) -> UsersConfig:
    """Read and validate the roster of memory spaces to serve."""

    return build_registry(settings).load()


__all__ = [
    "RegistryPort",
    "RegistrySourceUnavailable",
    "build_registry",
    "load_users_config",
    "resolve_static_registry_path",
]
