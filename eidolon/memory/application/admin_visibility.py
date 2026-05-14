"""Row visibility helpers for Admin / MCP façade (privacy drawer filtering)."""

from __future__ import annotations

from eidolon.memory.domain.wire import MemoryWireRecord


def admin_row_visible(rec: MemoryWireRecord, *, include_private: bool) -> bool:
    if include_private:
        return True
    if rec.metadata.get("wing") == "Wing_Privacy" or rec.user_id == "Wing_Privacy":
        return False
    return True
