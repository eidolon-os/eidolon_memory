"""MCP HTTP client helpers."""

from __future__ import annotations

import mcp.types as types

from eidolon.memory.infrastructure.mcp_http_client import decode_call_tool_json


def test_decode_unwraps_fastmcp_list_result():
    result = types.CallToolResult(
        content=[],
        structuredContent={"result": [{"key": "a"}]},
        isError=False,
    )
    assert decode_call_tool_json(result) == [{"key": "a"}]


def test_decode_preserves_dict_tool_payload():
    result = types.CallToolResult(
        content=[],
        structuredContent={"records": [], "total_hint": 0},
        isError=False,
    )
    payload = decode_call_tool_json(result)
    assert payload == {"records": [], "total_hint": 0}
