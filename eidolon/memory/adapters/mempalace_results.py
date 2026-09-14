"""Convert stored rows without discarding their identity or provenance."""

from __future__ import annotations

import json
from typing import Any

from eidolon.memory.domain.wire import MemoryWireRecord, parse_memory_datetime


def storage_record(
    drawer_id: str,
    text: str,
    metadata: dict[str, Any],
    *,
    memory_space_id: str | None = None,
    search: bool = True,
) -> MemoryWireRecord:
    """Keep the existing search/get value and key contracts, plus the real ID."""
    meta = dict(metadata)
    wing = str(meta.get("wing") or "default")
    room = str(meta.get("room") or "general")
    meta.update(wing=wing, room=room, _storage_id=drawer_id, _storage_metadata_verified=True)
    meta.setdefault("source", "mempalace-python")
    value: Any = text
    if search:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
    return MemoryWireRecord(
        memory_space_id=str(meta.get("memory_space_id") or memory_space_id or wing),
        key=room if search else drawer_id,
        value=value,
        metadata=meta,
        created_at=parse_memory_datetime(meta.get("created_at") or meta.get("filed_at")),
        updated_at=parse_memory_datetime(meta.get("updated_at")),
    )
