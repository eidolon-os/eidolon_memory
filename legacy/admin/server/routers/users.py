"""User lifecycle — list / create / init / start / stop.

Admin can spawn / kill agent_runner subprocesses it owns. Externally started
agents (supervisor) are visible via the port probe but show
``managed_by_admin=false`` and can NOT be stopped from here — we only signal
PIDs we know belong to admin's subprocess registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_manager import AgentProcessManager
from dependencies import AdminAuth, SettingsDep
from fastapi import APIRouter, HTTPException, Request
from mcp_client import mcp_http_url, probe_mcp_http
from schemas import (
    UserCreateRequest,
    UserDetail,
    UserMutateResponse,
    UsersListResponse,
)
from user_registry import palace_path_for_user
from users_writer import UsersYamlError, update_enabled, upsert_user

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.users import (
    UserEntry,
    load_users_config,
    resolve_users_file_path,
)
from eidolon.memory.infrastructure.palace_init import (
    PalaceInitError,
    ensure_palace_initialized,
    palace_is_initialized,
)

router = APIRouter(prefix="/users", tags=["users"])


def _manager(request: Request) -> AgentProcessManager:
    mgr = getattr(request.app.state, "agent_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="agent manager not initialized")
    return mgr


async def _build_detail(
    settings: MemorySettings,
    entry: UserEntry,
    manager: AgentProcessManager,
) -> UserDetail:
    palace = palace_path_for_user(settings, entry)
    url = mcp_http_url(settings, port=entry.port)
    reachable = await probe_mcp_http(url, settings=settings)
    managed = manager.status(entry.id)
    return UserDetail(
        user_id=entry.id,
        port=entry.port,
        enabled=entry.enabled,
        palace_path=palace,
        mcp_http_url=url,
        agent_reachable=reachable,
        palace_initialized=palace_is_initialized(Path(palace)),
        managed_by_admin=managed is not None,
        pid=managed.pid if managed else None,
        log_path=managed.log_path if managed else None,
    )


@router.get("", response_model=UsersListResponse)
async def list_users(
    _: AdminAuth,
    settings: SettingsDep,
    request: Request,
) -> UsersListResponse:
    manager = _manager(request)
    cfg = load_users_config(settings)
    details = await asyncio.gather(
        *[_build_detail(settings, u, manager) for u in cfg.users]
    )
    return UsersListResponse(
        users=list(details),
        users_yaml=str(resolve_users_file_path(settings)),
    )


@router.post("", response_model=UserMutateResponse, status_code=201)
async def create_user(
    body: UserCreateRequest,
    _: AdminAuth,
    settings: SettingsDep,
    request: Request,
) -> UserMutateResponse:
    entry = UserEntry(
        id=body.id,
        port=body.port,
        enabled=body.enabled,
        palace_path=body.palace_path or "",
    )
    path = resolve_users_file_path(settings)
    try:
        upsert_user(settings, path, entry)
    except UsersYamlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    detail_note = []
    if body.init_palace:
        try:
            await asyncio.to_thread(
                ensure_palace_initialized,
                entry.id,
                Path(palace_path_for_user(settings, entry)),
            )
            detail_note.append("palace initialized")
        except PalaceInitError as exc:
            raise HTTPException(status_code=400, detail=f"palace init failed: {exc}") from exc

    if body.auto_start and entry.enabled:
        manager = _manager(request)
        try:
            await manager.start(user_id=entry.id, port=entry.port)
            detail_note.append(f"agent spawned (pid {manager.status(entry.id).pid})")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"agent start failed: {exc}") from exc

    manager = _manager(request)
    detail = await _build_detail(settings, entry, manager)
    return UserMutateResponse(user=detail, message="; ".join(detail_note) or "created")


@router.post("/{user_id}/init", response_model=UserMutateResponse)
async def init_palace(
    user_id: str,
    _: AdminAuth,
    settings: SettingsDep,
    request: Request,
) -> UserMutateResponse:
    cfg = load_users_config(settings)
    entry = cfg.find(user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")
    palace = Path(palace_path_for_user(settings, entry))
    try:
        await asyncio.to_thread(ensure_palace_initialized, entry.id, palace)
    except PalaceInitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detail = await _build_detail(settings, entry, _manager(request))
    return UserMutateResponse(user=detail, message="palace initialized")


@router.post("/{user_id}/start", response_model=UserMutateResponse)
async def start_agent(
    user_id: str,
    _: AdminAuth,
    settings: SettingsDep,
    request: Request,
) -> UserMutateResponse:
    cfg = load_users_config(settings)
    entry = cfg.find(user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")
    if not entry.enabled:
        raise HTTPException(status_code=400, detail="user is disabled in users.yaml")

    # If port is already occupied by a non-admin process, refuse — we won't
    # double-bind. (Probe is best-effort: TIME_WAIT corner cases will simply
    # surface as a spawn failure.)
    if await probe_mcp_http(mcp_http_url(settings, port=entry.port), settings=settings):
        manager = _manager(request)
        if not manager.is_managed(entry.id):
            raise HTTPException(
                status_code=409,
                detail=f"port {entry.port} already in use (external agent or supervisor); "
                f"admin will not double-start",
            )

    manager = _manager(request)
    try:
        meta = await manager.start(user_id=entry.id, port=entry.port)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Give the agent a moment to bind the port; status snapshot then reflects
    # whether the warm path opened.
    for _ in range(15):
        await asyncio.sleep(0.4)
        if await probe_mcp_http(
            mcp_http_url(settings, port=entry.port), settings=settings
        ):
            break
    detail = await _build_detail(settings, entry, manager)
    return UserMutateResponse(user=detail, message=f"started (pid {meta.pid})")


@router.post("/{user_id}/stop", response_model=UserMutateResponse)
async def stop_agent(
    user_id: str,
    _: AdminAuth,
    settings: SettingsDep,
    request: Request,
) -> UserMutateResponse:
    cfg = load_users_config(settings)
    entry = cfg.find(user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown user {user_id!r}")
    manager = _manager(request)
    if not manager.is_managed(entry.id):
        raise HTTPException(
            status_code=409,
            detail="agent not managed by admin (started externally or already stopped)",
        )
    stopped = await manager.stop(entry.id)
    detail = await _build_detail(settings, entry, manager)
    return UserMutateResponse(
        user=detail,
        message="stopped" if stopped else "no managed process to stop",
    )


@router.post("/{user_id}/enable", response_model=UserMutateResponse)
async def toggle_enable(
    user_id: str,
    enabled: bool,
    _: AdminAuth,
    settings: SettingsDep,
    request: Request,
) -> UserMutateResponse:
    path = resolve_users_file_path(settings)
    try:
        update_enabled(settings, path, user_id, enabled)
    except UsersYamlError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    cfg = load_users_config(settings)
    entry = cfg.find(user_id)
    assert entry is not None
    detail = await _build_detail(settings, entry, _manager(request))
    return UserMutateResponse(user=detail, message=f"enabled={enabled}")
