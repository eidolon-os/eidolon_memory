"""Read the roster of memory spaces from a YAML file.

This is what lets the service run on its own. The Eidolon OS deployment gets its
roster from an admin service that owns owner/companion lifecycle; a standalone
deployment has no such service, and an operator declaring the spaces in a file is
the whole of what it needs.

Format::

    memory_spaces:
      - id: alice
        owner_id: alice            # optional; defaults to id
        companion_id: default      # optional
        enabled: true              # optional; defaults to true
        port: 10030                # optional; derived from id when omitted
        consolidator:              # optional; off unless present and enabled
          enabled: true
          interval_hours: 6

``port`` is normally left out — the same deterministic derivation the admin path
uses assigns one, which keeps a space on a stable port across restarts without an
operator having to track allocations.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.users import (
    ConsolidatorUserConfig,
    RegistrySourceUnavailable,
    UserEntry,
    UsersConfig,
    stable_realm_port,
)


class StaticFileRegistry:
    """Roster from a YAML file on disk."""

    def __init__(self, settings: MemorySettings, *, path: Path) -> None:
        self._settings = settings
        self._path = path

    def load(self) -> UsersConfig:
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise RegistrySourceUnavailable(
                f"static memory-space roster not found at {self._path}"
            ) from exc
        except (OSError, yaml.YAMLError) as exc:
            raise RegistrySourceUnavailable(
                f"static memory-space roster at {self._path} could not be read: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise RegistrySourceUnavailable(
                f"static memory-space roster at {self._path} must be a mapping"
            )

        declared = raw.get("memory_spaces")
        if declared is None:
            raise RegistrySourceUnavailable(
                f"static memory-space roster at {self._path} has no 'memory_spaces' key"
            )
        if not isinstance(declared, list):
            raise RegistrySourceUnavailable(
                f"'memory_spaces' in {self._path} must be a list"
            )

        entries: list[UserEntry] = []
        used_ports: set[int] = set()
        for item in declared:
            if not isinstance(item, dict):
                raise RegistrySourceUnavailable(
                    f"each entry in {self._path} must be a mapping; got {type(item).__name__}"
                )
            entry = self._entry(item, used_ports=used_ports)
            used_ports.add(entry.port)
            entries.append(entry)
        return UsersConfig(users=entries)

    def _entry(self, item: dict, *, used_ports: set[int]) -> UserEntry:
        space_id = str(item.get("id") or "").strip()
        if not space_id:
            raise RegistrySourceUnavailable(f"an entry in {self._path} has no 'id'")

        port = item.get("port")
        resolved_port = (
            int(port)
            if port is not None
            else stable_realm_port(
                space_id,
                base_port=self._settings.mcp_http.port,
                used_ports=used_ports,
            )
        )

        consolidator = item.get("consolidator")
        return UserEntry(
            id=space_id,
            owner_id=str(item.get("owner_id") or space_id).strip(),
            companion_id=(str(item.get("companion_id") or "").strip() or None),
            port=resolved_port,
            enabled=bool(item.get("enabled", True)),
            consolidator=(
                ConsolidatorUserConfig.model_validate(consolidator)
                if isinstance(consolidator, dict)
                else None
            ),
        )
