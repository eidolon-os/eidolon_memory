"""Shared semantic recall logic for MCP read tools and HTTP admin (same behavior)."""

from __future__ import annotations

import asyncio
from typing import Any

from eidolon.memory.adapters.recall_ranking import public_metadata, rank_records_by_similarity
from eidolon.memory.application.recall_filters import filter_voice_recall_hits
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import MemoryReader
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def wire_record_to_public_dict(rec: MemoryWireRecord) -> dict[str, Any]:
    payload = rec.model_dump(mode="json")
    payload["metadata"] = public_metadata(rec.metadata)
    return payload


def recall_record_visible_for_user(rec: MemoryWireRecord, user_id: str) -> bool:
    if rec.metadata.get("wing") == "Wing_Privacy" or rec.user_id == "Wing_Privacy":
        return False
    privacy = str(rec.metadata.get("privacy", "")).lower()
    if privacy in {"private", "do_not_recall"}:
        return False
    meta_user = str(rec.metadata.get("user_id", ""))
    return not meta_user or meta_user == user_id


def group_recall_context(
    records: list[MemoryWireRecord],
    *,
    kg_triples: list | None = None,
) -> str:
    """Compose context block. Vector fragments grouped by memory_type;
    KG triples (if any) appear as a separate section so the LLM can treat
    structured facts differently from semantic memories.
    """
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

    if kg_triples:
        from eidolon.memory.application.kg_recall import transcribe_triples

        if lines:
            lines.append("")
        lines.append(transcribe_triples(kg_triples))
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


async def recall_with_kg_fusion(
    backend: MemoryReader,
    settings: MemorySettings,
    *,
    query: str,
    user_id: str,
    top_k: int,
    kg: object | None,
    for_voice: bool = False,
    session_id: str = "",
    user_utterance: str = "",
    palace_path: str | None = None,
    include_sensitive_kg: bool = False,
) -> dict[str, list]:
    """KG plan §5: parallel vector + KG via ``asyncio.gather``.

    Returns ``{"vector": [MemoryWireRecord], "kg": [KgTripleRecord]}``. The KG
    side honors :data:`RecallPolicy.kg_timeout_seconds` (default 0.05s);
    timeout silently degrades to vector-only — never raises into the caller so
    LiveKit's 300ms budget stays intact.
    """
    vector_task = asyncio.create_task(
        search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            wing=None,
            room=None,
            for_voice=for_voice,
            session_id=session_id,
            user_utterance=user_utterance,
            palace_path=palace_path,
        )
    )

    kg_task: asyncio.Task | None = None
    if kg is not None and settings.recall.kg_in_recall:
        # voice path keeps the hard 50ms (LiveKit 300ms budget); non-voice gets
        # a more generous window because admin/IDE callers don't share the
        # LiveKit deadline and the vector path may saturate the to_thread
        # executor with ONNX work for many seconds on a cold first call.
        kg_timeout = (
            settings.recall.kg_timeout_seconds if for_voice else 1.0
        )
        kg_task = asyncio.create_task(
            _kg_path_with_timeout(
                kg,
                query=query,
                max_entities=settings.recall.kg_max_entities,
                window_days=settings.recall.kg_window_days,
                max_triples_per_entity=settings.recall.kg_max_triples_per_entity,
                timeout_s=kg_timeout,
                include_sensitive=include_sensitive_kg,
            )
        )

    vector_records = await vector_task
    kg_records = await kg_task if kg_task is not None else []
    return {"vector": vector_records, "kg": kg_records}


async def _kg_path_with_timeout(
    kg,
    *,
    query: str,
    max_entities: int,
    window_days: int,
    max_triples_per_entity: int,
    timeout_s: float,
    include_sensitive: bool,
) -> list:
    """Route entity candidates → one combined SQL; degrade silently on timeout.

    ``kg.match_entities_for_query`` owns the naming-convention bridge
    (literal + prefix-strip), so this layer just asks "give me candidates"
    without knowing about ``pet:`` / ``place:`` / ``mother:`` prefixes.
    Future synonym / alias support extends that method, not this caller.
    """
    from eidolon.memory.application.kg_recall import query_kg_for_recall

    try:
        async def _inner():
            candidates = await kg.match_entities_for_query(query, cap=max_entities)
            if not candidates:
                return []
            return await query_kg_for_recall(
                kg,
                entity_names=candidates,
                window_days=window_days,
                max_triples_per_entity=max_triples_per_entity,
                include_sensitive=include_sensitive,
            )

        return await asyncio.wait_for(_inner(), timeout=timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("kg_recall_timeout", timeout_s=timeout_s)
        return []
    except Exception as exc:
        log.warning("kg_recall_failed", error=str(exc))
        return []


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
            backend=backend,
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

    return rank_records_by_similarity(hits, top_k=top_k)


async def _search_voice_shared_embedding(
    palace_path: str,
    settings: MemorySettings,
    *,
    backend: MemoryReader,
    query: str,
    wings: list[str],
    room: str | None,
    top_k: int,
    user_id: str,
) -> list[MemoryWireRecord]:
    """Voice fast-path: one ONNX embed, parallel ``collection.query`` per wing.

    D1: chroma calls bypass ``MemoryBackend.search`` for the shared-embedding
    optimization, so we must acquire ``LockedBackend.lock`` here to keep the
    single-writer-single-reader contract. Non-locked backends (tests) fall back
    to running unlocked.
    """
    import asyncio

    from eidolon.memory.adapters.locked_backend import LockedBackend
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

    if isinstance(backend, LockedBackend):
        async with backend.lock:
            records = await asyncio.to_thread(_run)
    else:
        records = await asyncio.to_thread(_run)
    return [r for r in records if recall_record_visible_for_user(r, user_id)]
