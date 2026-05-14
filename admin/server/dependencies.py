"""FastAPI deps: lazy MemPalace backend and optional Bearer auth."""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config.memory_settings import MemorySettings, get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory

_backend: MemPalacePythonBackend | None = None
_ingest_lock = asyncio.Lock()


async def get_backend() -> MemPalacePythonBackend:
    global _backend
    if _backend is not None:
        return _backend
    settings = get_memory_settings()
    palace = str(resolve_palace_directory(settings))
    _backend = MemPalacePythonBackend(settings, palace)
    return _backend


def get_memory_settings_cached() -> MemorySettings:
    return get_memory_settings()


def get_serialize_lock() -> asyncio.Lock:
    return _ingest_lock


async def verify_admin_optional(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.environ.get("EIDOLON_MEMORY_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    got = authorization[7:].strip()
    if got != expected:
        raise HTTPException(status_code=403, detail="invalid token")


AdminAuth = Annotated[None, Depends(verify_admin_optional)]
BackendDep = Annotated[MemPalacePythonBackend, Depends(get_backend)]
SettingsDep = Annotated[MemorySettings, Depends(get_memory_settings_cached)]
