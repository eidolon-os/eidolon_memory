"""Row-visibility helper used by the MCP ``list`` tool.

This is **privacy filtering**, not access control / authentication — it just
hides ``Wing_Privacy`` drawers from default listings so the caller has to
opt in with ``include_private=True`` to see them. Anyone holding the MCP
session can opt in; the gate is intent, not authorization.
"""

from __future__ import annotations

from eidolon.memory.domain.wire import MemoryWireRecord


def row_visible_to_listing(rec: MemoryWireRecord, *, include_private: bool) -> bool:
    """Return True if the row should appear in a listing call.

    ``include_private=False`` (default) hides ``Wing_Privacy`` rows.
    """
    if include_private:
        return True
    if rec.metadata.get("wing") == "Wing_Privacy" or rec.user_id == "Wing_Privacy":
        return False
    return True
