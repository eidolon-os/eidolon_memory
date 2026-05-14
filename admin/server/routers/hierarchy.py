"""MemPalace hierarchy via MCP snapshot (bounded scan)."""

from __future__ import annotations

from dependencies import AdminAuth, McpSessionDep
from fastapi import APIRouter, HTTPException, Query
from schemas import MemPalaceHierarchyResponse

from eidolon.memory.infrastructure.admin_mcp_client import call_tool_json

router = APIRouter(prefix="/hierarchy", tags=["hierarchy"])


@router.get("", response_model=MemPalaceHierarchyResponse)
@router.get("/", response_model=MemPalaceHierarchyResponse)
async def mempalace_hierarchy(
    _: AdminAuth,
    mcp: McpSessionDep,
    max_records: int = Query(8000, ge=50, le=50_000, description="最多扫描的记录条数。"),
    max_drawers_per_room: int = Query(
        48,
        ge=4,
        le=400,
        description="每个 Room 下列出的抽屉预览条数上限；完整计数见 drawer_count。",
    ),
) -> MemPalaceHierarchyResponse:
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
