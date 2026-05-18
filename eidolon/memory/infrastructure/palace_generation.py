"""Atomic palace generation counter for cross-process read invalidation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class GenerationInfo:
    generation: int
    updated_at: str
    writer: str


def default_generation_path(palace_path: str | Path) -> Path:
    return Path(palace_path).expanduser().resolve() / ".eidolon" / "palace_generation"


def resolve_generation_path(palace_path: str | Path, configured: str = "") -> Path:
    if configured.strip():
        return Path(configured).expanduser().resolve()
    return default_generation_path(palace_path)


def read_generation(path: str | Path) -> GenerationInfo:
    """Read generation file; missing or corrupt → generation 0."""
    p = Path(path)
    if not p.is_file():
        return GenerationInfo(generation=0, updated_at="", writer="")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return GenerationInfo(generation=0, updated_at="", writer="")
        gen = int(data.get("generation", 0) or 0)
        return GenerationInfo(
            generation=max(0, gen),
            updated_at=str(data.get("updated_at", "")),
            writer=str(data.get("writer", "")),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return GenerationInfo(generation=0, updated_at="", writer="")


def bump_generation(
    path: str | Path,
    *,
    writer: str = "eidolon-memory-worker",
) -> GenerationInfo:
    """Atomically increment generation (write tmp + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = read_generation(p)
    info = GenerationInfo(
        generation=current.generation + 1,
        updated_at=datetime.now(timezone.utc).isoformat(),
        writer=writer,
    )
    payload = json.dumps(
        {
            "generation": info.generation,
            "updated_at": info.updated_at,
            "writer": info.writer,
        },
        ensure_ascii=False,
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix="palace_generation.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return info


def generation_changed(path: str | Path, seen: int) -> bool:
    return read_generation(path).generation != seen
