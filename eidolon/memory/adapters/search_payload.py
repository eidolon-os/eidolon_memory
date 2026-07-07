"""Normalize MemPalace search payloads into ``MemoryWireRecord``."""

from __future__ import annotations

import json
from typing import Any

from eidolon.memory.adapters.recall_ranking import vector_fields_from_hit
from eidolon.memory.domain.wire import MemoryWireRecord, parse_memory_datetime


def parse_search_tool_payload(
    data: Any,
    *,
    default_memory_space_id: str | None = None,
) -> list[MemoryWireRecord]:
    """Parse MCP ``search_drawers`` (or equivalent) JSON into wire records.

    ``default_memory_space_id`` is the authoritative memory space of the palace
    the hits came from. MemPalace's vector search drops custom metadata, so a
    hit rarely carries its own ``memory_space_id``; callers that know the space
    (the single-palace backend, the voice recall path) pass it here so records
    get the real id instead of falling back to the wing name — which would then
    fail recall's ``memory_space_id`` visibility gate and drop every vector hit.
    """
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
        similarity, internal = vector_fields_from_hit(r)
        raw_meta = r.get("metadata")
        meta: dict[str, Any] = raw_meta.copy() if isinstance(raw_meta, dict) else {}
        meta.update(
            {
                "wing": wing,
                "room": room,
                "source_file": str(r.get("source_file", "")),
                "similarity": round(similarity, 4),
            }
        )
        # Preserve the stored write-time ``source`` (e.g. "user-confirmed",
        # "consolidator") that recall ranking + theme rendering key off.
        # Only stamp "mcp" when the hit carried no source — "mcp" is just
        # "this came back via the search tool", redundant when real
        # provenance exists.
        meta.setdefault("source", "mcp")
        meta.update(internal)
        status = r.get("status", r.get("room_status"))
        if status is not None:
            meta["room_status"] = status
        created_at = parse_memory_datetime(
            r.get("created_at") or meta.get("created_at") or meta.get("filed_at")
        )
        updated_at = parse_memory_datetime(r.get("updated_at") or meta.get("updated_at"))
        out.append(
            MemoryWireRecord(
                memory_space_id=str(
                    meta.get("memory_space_id") or default_memory_space_id or wing or "default"
                ),
                key=room or "general",
                value=value,
                metadata=meta,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
    return out
