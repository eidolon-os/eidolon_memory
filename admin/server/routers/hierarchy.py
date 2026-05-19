"""MemPalace hierarchy via per-user MCP snapshot."""

from __future__ import annotations

from dependencies import AdminAuth, McpSessionDep
from fastapi import APIRouter, HTTPException, Query
from mcp_client import call_tool_json
from schemas import MemPalaceHierarchyResponse

router = APIRouter(prefix="/hierarchy", tags=["hierarchy"])


@router.get("", response_model=MemPalaceHierarchyResponse)
@router.get("/", response_model=MemPalaceHierarchyResponse)
async def mempalace_hierarchy(
    _: AdminAuth,
    mcp: McpSessionDep,
    user_id: str = Query(..., description="users.yaml agent id"),
    max_records: int = Query(8000, ge=50, le=50_000),
    max_drawers_per_room: int = Query(48, ge=4, le=400),
) -> MemPalaceHierarchyResponse:
    del user_id
    args = {
        "max_records": max_records,
        "max_drawers_per_room": max_drawers_per_room,
    }
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_hierarchy_snapshot", args)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="unexpected MCP hierarchy payload")
    return MemPalaceHierarchyResponse.model_validate(payload)
