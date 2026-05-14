"""Health/status endpoint for Admin UI."""

from __future__ import annotations

from dependencies import AdminAuth, SettingsDep
from fastapi import APIRouter
from schemas import HealthResponse

from eidolon.memory.config.palace_directory import resolve_palace_directory

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def health(_: AdminAuth, settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        ok=True,
        palace_path=str(resolve_palace_directory(settings)),
        steward_mode=settings.steward.mode,
    )
