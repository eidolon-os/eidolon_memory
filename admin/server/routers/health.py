"""Health/status for Admin UI (D1 per-user agents)."""

from __future__ import annotations

from dependencies import AdminAuth, SettingsDep
from fastapi import APIRouter
from schemas import HealthResponse, UserStatusOut
from user_registry import list_enabled_users, user_agent_status

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def health(_: AdminAuth, settings: SettingsDep) -> HealthResponse:
    users_cfg = list_enabled_users(settings)
    default_id = users_cfg[0].id
    statuses: list[UserStatusOut] = []
    for entry in users_cfg:
        raw = await user_agent_status(settings, entry)
        statuses.append(UserStatusOut.model_validate(raw))
    return HealthResponse(
        ok=True,
        steward_mode=settings.steward.mode,
        default_user_id=default_id,
        users=statuses,
    )
