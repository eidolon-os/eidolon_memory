"""MCP introspection — list tools exposed by the agent_runner control plane."""

from __future__ import annotations

from typing import Any

from dependencies import AdminAuth, McpSessionDep
from fastapi import APIRouter, HTTPException
from schemas import McpToolOut, McpToolsResponse

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/tools", response_model=McpToolsResponse)
async def list_tools(_: AdminAuth, mcp: McpSessionDep) -> McpToolsResponse:
    try:
        result = await mcp.list_tools()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    tools: list[McpToolOut] = []
    for t in getattr(result, "tools", []) or []:
        schema = getattr(t, "inputSchema", None)
        tools.append(
            McpToolOut(
                name=str(getattr(t, "name", "")),
                description=str(getattr(t, "description", "") or ""),
                input_schema=schema if isinstance(schema, dict) else _schema_to_dict(schema),
            )
        )
    return McpToolsResponse(tools=tools, count=len(tools))


def _schema_to_dict(schema: Any) -> dict[str, Any]:
    if schema is None:
        return {}
    if hasattr(schema, "model_dump"):
        return schema.model_dump(mode="json")
    if hasattr(schema, "__dict__"):
        return dict(schema.__dict__)
    return {}
