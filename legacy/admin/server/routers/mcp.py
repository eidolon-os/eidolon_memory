"""MCP introspection — list tools exposed by the agent_runner control plane.

Per-request session (no caching) — see ``mcp_call.list_user_mcp_tools``.
"""

from __future__ import annotations

from dependencies import AdminAuth, SettingsDep
from fastapi import APIRouter, Query
from mcp_call import list_user_mcp_tools
from schemas import McpToolOut, McpToolsResponse

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/tools", response_model=McpToolsResponse)
async def list_tools(
    _: AdminAuth,
    settings: SettingsDep,
    user_id: str = Query(..., description="users.yaml agent id"),
) -> McpToolsResponse:
    raw = await list_user_mcp_tools(settings, user_id)
    tools = [McpToolOut.model_validate(t) for t in raw]
    return McpToolsResponse(tools=tools, count=len(tools))
