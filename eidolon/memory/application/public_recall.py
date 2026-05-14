"""Shared semantic recall logic for MCP read tools and HTTP admin (same behavior)."""

from __future__ import annotations

from typing import Any

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import MemoryReader
from eidolon.memory.domain.wire import MemoryWireRecord


def wire_record_to_public_dict(rec: MemoryWireRecord) -> dict[str, Any]:
    return rec.model_dump(mode="json")


def recall_record_visible_for_user(rec: MemoryWireRecord, user_id: str) -> bool:
    if rec.metadata.get("wing") == "Wing_Privacy" or rec.user_id == "Wing_Privacy":
        return False
    privacy = str(rec.metadata.get("privacy", "")).lower()
    if privacy in {"private", "do_not_recall"}:
        return False
    meta_user = str(rec.metadata.get("user_id", ""))
    return not meta_user or meta_user == user_id


def group_recall_context(records: list[MemoryWireRecord]) -> str:
    groups: dict[str, list[str]] = {
        "个人画像与健康": [],
        "人机互动": [],
        "关系": [],
        "情绪": [],
        "愿景与目标": [],
        "工作学习": [],
        "生活方式与近况": [],
    }
    for rec in records:
        kind = str(rec.metadata.get("memory_type", "")).lower()
        text = str(rec.value)
        if kind == "interaction":
            groups["人机互动"].append(text)
        elif kind == "goal":
            groups["愿景与目标"].append(text)
        elif kind in {"profile", "health"}:
            groups["个人画像与健康"].append(text)
        elif kind == "relationship":
            groups["关系"].append(text)
        elif kind == "emotion":
            groups["情绪"].append(text)
        elif kind == "work":
            groups["工作学习"].append(text)
        elif kind in {"preference", "life"}:
            groups["生活方式与近况"].append(text)
        else:
            groups["生活方式与近况"].append(text)
    lines: list[str] = []
    for title, items in groups.items():
        if items:
            lines.append(f"{title}:")
            lines.extend(f"- {item}" for item in items[:4])
    return "\n".join(lines)


async def search_all_wings_mcp_style(
    backend: MemoryReader,
    settings: MemorySettings,
    *,
    query: str,
    user_id: str,
    top_k: int,
    wing: str | None,
    room: str | None,
) -> list[MemoryWireRecord]:
    """Match MCP ``eidolon_memory_search``: search configured wings, visibility filter, rank."""
    wings = [wing] if wing else [w.id for w in settings.wings if w.id != "Wing_Privacy"]
    hits: list[MemoryWireRecord] = []
    uid = user_id or "default"
    for wing_id in wings:
        found = await backend.search(query, wing=wing_id, n_results=top_k, room=room)
        hits.extend(r for r in found if recall_record_visible_for_user(r, uid))
    hits.sort(
        key=lambda r: (
            int(r.metadata.get("importance", 0) or 0),
            -float(r.metadata.get("score", 1.0) or 1.0),
        ),
        reverse=True,
    )
    return hits[:top_k]
