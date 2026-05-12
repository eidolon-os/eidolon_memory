"""Normalize MemPalace MCP JSON into ``MemoryWireRecord``."""

from __future__ import annotations

import json
from typing import Any

from eidolon.memory.domain.wire import MemoryWireRecord


def parse_search_tool_payload(data: Any) -> list[MemoryWireRecord]:
    """Parse MCP ``search_drawers`` (or equivalent) JSON into wire records."""
    if data is None:
        return []
    rows: list[dict[str, Any]]
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        inner = data.get("results")
        if isinstance(inner, list):
            rows = [r for r in inner if isinstance(r, dict)]
        else:
            rows = [data]
    else:
        return []

    out: list[MemoryWireRecord] = []
    for r in rows:
        wing = str(r.get("wing", r.get("user_id", "")))
        room = str(r.get("room", r.get("key", "")))
        text = r.get("text", r.get("content", r.get("value", "")))
        if isinstance(text, dict):
            value: Any = text
        else:
            try:
                value = json.loads(str(text)) if text else ""
            except (json.JSONDecodeError, TypeError):
                value = text if text is not None else ""
        score = r.get("distance", r.get("score"))
        meta: dict[str, Any] = {
            "wing": wing,
            "room": room,
            "source": "mcp",
            "source_file": str(r.get("source_file", "")),
        }
        if score is not None:
            try:
                meta["score"] = float(score)
            except (TypeError, ValueError):
                meta["score"] = score
        status = r.get("status", r.get("room_status"))
        if status is not None:
            meta["room_status"] = status
        out.append(
            MemoryWireRecord(
                user_id=wing or "default",
                key=room or "general",
                value=value,
                metadata=meta,
            )
        )
    return out
