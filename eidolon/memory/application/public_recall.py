"""Shared semantic recall logic for MCP read tools and HTTP admin (same behavior)."""

from __future__ import annotations

import asyncio
from typing import Any

from eidolon.memory.application.recall_filters import filter_voice_recall_hits
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


def _resolve_wings(
    settings: MemorySettings,
    *,
    wing: str | None,
    for_voice: bool,
) -> list[str]:
    if wing:
        return [wing]
    if for_voice and settings.recall.voice_wings:
        return list(settings.recall.voice_wings)
    return [w.id for w in settings.wings if w.id != "Wing_Privacy"]


def _effective_wing_parallel(settings: MemorySettings, *, for_voice: bool) -> int:
    from eidolon.memory.infrastructure.cpu_env import recommend_max_wing_parallel

    if for_voice:
        return recommend_max_wing_parallel(settings, role="livekit")
    explicit = settings.runtime.read.max_wing_parallel
    if explicit > 0:
        return explicit
    return max(1, min(4, __import__("os").cpu_count() or 4 // 2))


async def search_all_wings_mcp_style(
    backend: MemoryReader,
    settings: MemorySettings,
    *,
    query: str,
    user_id: str,
    top_k: int,
    wing: str | None,
    room: str | None,
    for_voice: bool = False,
    session_id: str = "",
    user_utterance: str = "",
    palace_path: str | None = None,
) -> list[MemoryWireRecord]:
    """Search configured wings in parallel, filter, rank, and cap top_k."""
    wings = _resolve_wings(settings, wing=wing, for_voice=for_voice)
    uid = user_id or "default"

    if (
        for_voice
        and wings
        and settings.runtime.read.shared_query_embedding
        and palace_path
    ):
        hits = await _search_voice_shared_embedding(
            palace_path,
            settings,
            query=query,
            wings=wings,
            room=room,
            top_k=top_k,
            user_id=uid,
        )
    else:
        parallel = _effective_wing_parallel(settings, for_voice=for_voice)
        sem = asyncio.Semaphore(parallel)

        async def _one(wing_id: str) -> list[MemoryWireRecord]:
            async with sem:
                found = await backend.search(query, wing=wing_id, n_results=top_k, room=room)
                return [r for r in found if recall_record_visible_for_user(r, uid)]

        batches = await asyncio.gather(*[_one(wid) for wid in wings], return_exceptions=True)
        hits = []
        for batch in batches:
            if isinstance(batch, BaseException):
                continue
            hits.extend(batch)

    if for_voice:
        hits = filter_voice_recall_hits(
            hits,
            settings,
            session_id=session_id,
            user_utterance=user_utterance,
        )

    def _score_for_sort(rec: MemoryWireRecord) -> float:
        raw = rec.metadata.get("score", 1.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 1.0

    hits.sort(
        key=lambda r: (int(r.metadata.get("importance", 0) or 0), -_score_for_sort(r)),
        reverse=True,
    )
    return hits[:top_k]


async def _search_voice_shared_embedding(
    palace_path: str,
    settings: MemorySettings,
    *,
    query: str,
    wings: list[str],
    room: str | None,
    top_k: int,
    user_id: str,
) -> list[MemoryWireRecord]:
    import asyncio

    from eidolon.memory.adapters.mempalace_fast_search import search_memories_shared_embedding
    from eidolon.memory.adapters.search_payload import parse_search_tool_payload

    def _run() -> list[MemoryWireRecord]:
        raw = search_memories_shared_embedding(
            query,
            palace_path,
            wings=wings,
            room=room,
            n_results=top_k,
            skip_closets=settings.runtime.read.voice_skip_closets,
        )
        return parse_search_tool_payload({"results": raw})

    records = await asyncio.to_thread(_run)
    return [r for r in records if recall_record_visible_for_user(r, user_id)]
