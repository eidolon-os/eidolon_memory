"""Knowledge graph + recall — all calls go through agent_runner MCP.

Writes (add / invalidate) hit MCP tools that themselves publish
``KgAddTripleCommand`` / ``KgInvalidateCommand`` to NATS → worker applies under
``LockedKnowledgeGraph``. The admin process never touches the KG SQLite file.
"""

from __future__ import annotations

from typing import Any

from dependencies import AdminAuth, McpSessionDep, SettingsDep
from fastapi import APIRouter, HTTPException, Query
from mcp_client import call_tool_json
from schemas import (
    KgEntityResponse,
    KgInvalidateRequest,
    KgPredicates,
    KgStats,
    KgTimelineResponse,
    KgTripleAddRequest,
    KgTripleOut,
    KgWriteResult,
    RecallRequest,
    RecallResponse,
)
from user_registry import resolve_user_entry

router = APIRouter(prefix="/kg", tags=["kg"])


def _ensure_dict(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"unexpected MCP {what} payload")
    return payload


def _triples_from(payload: dict[str, Any], key: str) -> list[KgTripleOut]:
    raw = payload.get(key) or []
    if not isinstance(raw, list):
        raise HTTPException(status_code=502, detail=f"MCP {key} not a list")
    return [KgTripleOut.model_validate(r) for r in raw]


@router.get("/predicates", response_model=KgPredicates)
async def get_predicates(_: AdminAuth, mcp: McpSessionDep) -> KgPredicates:
    payload = _ensure_dict(
        await call_tool_json(mcp, "eidolon_memory_kg_predicates", {}),
        "kg_predicates",
    )
    return KgPredicates.model_validate(payload)


@router.get("/stats", response_model=KgStats)
async def get_stats(_: AdminAuth, mcp: McpSessionDep) -> KgStats:
    payload = _ensure_dict(
        await call_tool_json(mcp, "eidolon_memory_kg_stats", {}),
        "kg_stats",
    )
    return KgStats.model_validate(payload)


@router.get("/entity/{name}", response_model=KgEntityResponse)
async def query_entity(
    name: str,
    _: AdminAuth,
    mcp: McpSessionDep,
    as_of: str | None = Query(None),
    direction: str = Query("outgoing", pattern="^(outgoing|incoming|both)$"),
    include_sensitive: bool = Query(False),
) -> KgEntityResponse:
    args: dict[str, Any] = {
        "name": name,
        "direction": direction,
        "include_sensitive": include_sensitive,
    }
    if as_of:
        args["as_of"] = as_of
    payload = _ensure_dict(
        await call_tool_json(mcp, "eidolon_memory_kg_query_entity", args),
        "kg_query_entity",
    )
    return KgEntityResponse(
        entity=str(payload.get("entity") or name),
        as_of=payload.get("as_of"),
        direction=str(payload.get("direction") or direction),
        triples=_triples_from(payload, "triples"),
    )


@router.get("/timeline", response_model=KgTimelineResponse)
async def get_timeline(
    _: AdminAuth,
    mcp: McpSessionDep,
    entity_name: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    limit: int = Query(100, ge=1, le=2000),
    include_sensitive: bool = Query(False),
) -> KgTimelineResponse:
    args: dict[str, Any] = {
        "limit": limit,
        "include_sensitive": include_sensitive,
    }
    if entity_name:
        args["entity_name"] = entity_name
    if since:
        args["since"] = since
    if until:
        args["until"] = until
    payload = _ensure_dict(
        await call_tool_json(mcp, "eidolon_memory_kg_timeline", args),
        "kg_timeline",
    )
    return KgTimelineResponse(
        entity_name=payload.get("entity_name"),
        since=payload.get("since"),
        until=payload.get("until"),
        events=_triples_from(payload, "events"),
    )


@router.post("/triples", response_model=KgWriteResult, status_code=202)
async def add_triple(
    body: KgTripleAddRequest,
    _: AdminAuth,
    settings: SettingsDep,
    mcp: McpSessionDep,
) -> KgWriteResult:
    resolve_user_entry(settings, body.user_id)
    args: dict[str, Any] = {
        "subject": body.subject,
        "predicate": body.predicate,
        "object": body.object,
        "confidence": body.confidence,
        "wait_visible_seconds": body.wait_visible_seconds,
    }
    if body.valid_from:
        args["valid_from"] = body.valid_from
    if body.valid_to:
        args["valid_to"] = body.valid_to
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_kg_add_triple", args)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KgWriteResult.model_validate(_ensure_dict(payload, "kg_add_triple"))


@router.post("/invalidations", response_model=KgWriteResult, status_code=202)
async def invalidate_triple(
    body: KgInvalidateRequest,
    _: AdminAuth,
    settings: SettingsDep,
    mcp: McpSessionDep,
) -> KgWriteResult:
    resolve_user_entry(settings, body.user_id)
    args: dict[str, Any] = {
        "subject": body.subject,
        "predicate": body.predicate,
        "object": body.object,
        "wait_visible_seconds": body.wait_visible_seconds,
    }
    if body.ended:
        args["ended"] = body.ended
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_kg_invalidate", args)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KgWriteResult.model_validate(_ensure_dict(payload, "kg_invalidate"))


# ─── Recall (fused vector + KG) ─────────────────────────────────────────


recall_router = APIRouter(prefix="/recall", tags=["recall"])


@recall_router.post("", response_model=RecallResponse)
async def recall_context(
    body: RecallRequest,
    _: AdminAuth,
    mcp: McpSessionDep,
) -> RecallResponse:
    args: dict[str, Any] = {
        "query": body.query,
        "top_k": body.top_k,
        "voice": body.voice,
        "include_sensitive_kg": body.include_sensitive_kg,
    }
    if body.include_kg is not None:
        args["include_kg"] = body.include_kg
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_recall_context", args)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data = _ensure_dict(payload, "recall_context")
    return RecallResponse(
        context=str(data.get("context") or ""),
        kg_triples=[KgTripleOut.model_validate(t) for t in (data.get("kg_triples") or [])],
        records=list(data.get("records") or []),
    )
