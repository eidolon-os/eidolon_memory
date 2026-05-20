"""MemPalace hierarchy via per-user agent_runner MCP snapshot."""

from __future__ import annotations

from dependencies import AdminAuth, SettingsDep
from fastapi import APIRouter, HTTPException, Query
from mcp_call import call_user_mcp
from schemas import MemPalaceHierarchyResponse

router = APIRouter(prefix="/hierarchy", tags=["hierarchy"])


@router.get("", response_model=MemPalaceHierarchyResponse)
@router.get("/", response_model=MemPalaceHierarchyResponse)
async def mempalace_hierarchy(
    _: AdminAuth,
    settings: SettingsDep,
    user_id: str = Query(..., description="users.yaml agent id"),
    max_records: int = Query(8000, ge=50, le=50_000),
    max_drawers_per_room: int = Query(48, ge=4, le=400),
) -> MemPalaceHierarchyResponse:
    args = {
        "max_records": max_records,
        "max_drawers_per_room": max_drawers_per_room,
    }
    payload = await call_user_mcp(
        settings, user_id, "eidolon_memory_hierarchy_snapshot", args
    )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="unexpected MCP hierarchy payload")
    return MemPalaceHierarchyResponse.model_validate(payload)
