"""Resolve D1 per-user agent_runner endpoints from users.yaml."""

from __future__ import annotations

from fastapi import HTTPException

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.config.palace_directory import resolve_palace_for_user
from eidolon.memory.config.users import UserEntry, load_users_config

from mcp_client import mcp_http_url, probe_mcp_http


def list_enabled_users(settings: MemorySettings) -> list[UserEntry]:
    users = load_users_config(settings).enabled_users()
    if users:
        return users
    return [
        UserEntry(
            id="default",
            port=settings.mcp_http.port,
            enabled=True,
        )
    ]


def resolve_user_entry(settings: MemorySettings, user_id: str | None) -> UserEntry:
    uid = (user_id or "").strip()
    cfg = load_users_config(settings)
    if not uid:
        enabled = cfg.enabled_users()
        if not enabled:
            fallback = list_enabled_users(settings)
            return fallback[0]
        return enabled[0]
    found = cfg.find(uid)
    if found is None:
        raise HTTPException(status_code=404, detail=f"unknown user_id {uid!r} in users.yaml")
    if not found.enabled:
        raise HTTPException(status_code=403, detail=f"user {uid!r} is disabled")
    return found


def palace_path_for_user(settings: MemorySettings, entry: UserEntry) -> str:
    if entry.palace_path.strip():
        return entry.palace_path.strip()
    return str(resolve_palace_for_user(settings, entry.id))


async def user_agent_status(settings: MemorySettings, entry: UserEntry) -> dict[str, object]:
    url = mcp_http_url(settings, port=entry.port)
    reachable = await probe_mcp_http(url, settings=settings)
    palace = palace_path_for_user(settings, entry)
    status: dict[str, object] = {
        "user_id": entry.id,
        "port": entry.port,
        "enabled": entry.enabled,
        "palace_path": palace,
        "mcp_http_url": url,
        "agent_reachable": reachable,
    }
    if reachable:
        try:
            from mcp_client import call_tool_json, mcp_http_session

            async with mcp_http_session(url, settings=settings, connect_attempts=2) as session:
                tool_status = await call_tool_json(session, "eidolon_memory_status", {})
                if isinstance(tool_status, dict):
                    status["runner_status"] = tool_status
        except Exception as exc:
            status["runner_status_error"] = str(exc)
    return status
