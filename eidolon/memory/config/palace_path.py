"""Resolve MemPalace palace directory without the daemon ConfigManager."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _resolve_data_dir() -> Path:
    """Match daemon ``resolve_data_dir`` priority: env then default ``~/eidolon``."""
    raw = os.environ.get("EIDOLON_SHARED__DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / "eidolon").resolve()


def _palace_path_from_config_yaml() -> Path | None:
    """If ``EIDOLON_MEMORY_CONFIG_YAML`` is set, read ``shared.mempalace.palace_path``."""
    yaml_path = os.environ.get("EIDOLON_MEMORY_CONFIG_YAML", "").strip()
    if not yaml_path:
        return None
    path = Path(yaml_path).expanduser()
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    shared = data.get("shared") or {}
    mempalace = shared.get("mempalace") or {}
    rel = mempalace.get("palace_path")
    if not rel:
        return None
    base = _resolve_data_dir()
    p = Path(str(rel))
    if p.is_absolute():
        return p.resolve()
    return (base / rel).resolve()


def resolve_palace_path(raw: str | Path | None = None) -> Path:
    """Resolve palace dir: arg, env, optional project YAML, then ``~/eidolon/mempalace``."""
    if raw:
        p = Path(raw).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    env = os.environ.get("EIDOLON_MEMORY_PALACE_PATH", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    try:
        from_yaml = _palace_path_from_config_yaml()
        if from_yaml is not None:
            p = from_yaml
        else:
            p = (Path.home() / "eidolon" / "mempalace").resolve()
    except Exception:
        p = (Path.home() / "eidolon" / "mempalace").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def palace_path_cli_override() -> str | None:
    """Explicit palace path for server entrypoint (env or ``EIDOLON_MEMORY_CONFIG_YAML`` only)."""
    env = os.environ.get("EIDOLON_MEMORY_PALACE_PATH", "").strip()
    if env:
        return env
    from_yaml = _palace_path_from_config_yaml()
    return str(from_yaml) if from_yaml is not None else None
