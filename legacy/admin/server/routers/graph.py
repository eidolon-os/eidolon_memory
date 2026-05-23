"""MemPalace graph snapshots — fetched via per-user agent_runner MCP (D1)."""

from __future__ import annotations

from dependencies import AdminAuth, SettingsDep
from fastapi import APIRouter, HTTPException, Query
from graph_service import knowledge_graph_snapshot, palace_graph_snapshot
from schemas import KnowledgeGraphSnapshot, PalaceGraphSnapshot
from user_registry import palace_path_for_user, resolve_user_entry

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("/knowledge", response_model=KnowledgeGraphSnapshot)
async def get_knowledge_graph(
    _: AdminAuth,
    settings: SettingsDep,
    user_id: str = Query(..., description="users.yaml agent id"),
    max_triples: int = Query(400, ge=10, le=2000),
    current_only: bool = Query(True, description="Only facts without valid_to"),
    entity: str | None = Query(None, description="Filter to one entity name"),
    include_sensitive: bool = Query(False, description="Include health predicates"),
) -> KnowledgeGraphSnapshot:
    entry = resolve_user_entry(settings, user_id)
    palace = palace_path_for_user(settings, entry)
    payload = await knowledge_graph_snapshot(
        settings,
        user_id,
        palace_path=palace,
        max_triples=max_triples,
        current_only=current_only,
        entity=entity,
        include_sensitive=include_sensitive,
    )
    return KnowledgeGraphSnapshot.model_validate(payload)


@router.get("/palace", response_model=PalaceGraphSnapshot)
async def get_palace_graph(
    _: AdminAuth,
    settings: SettingsDep,
    user_id: str = Query(..., description="users.yaml agent id"),
    max_nodes: int = Query(120, ge=10, le=500),
    max_edges: int = Query(200, ge=10, le=1000),
) -> PalaceGraphSnapshot:
    entry = resolve_user_entry(settings, user_id)
    palace = palace_path_for_user(settings, entry)
    payload = await palace_graph_snapshot(
        settings,
        user_id,
        palace_path=palace,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    if not payload.get("available"):
        reason = payload.get("reason") or "palace graph unavailable"
        if reason and ("empty" in reason.lower() or "missing" in reason.lower()):
            return PalaceGraphSnapshot.model_validate(payload)
        raise HTTPException(status_code=502, detail=reason)
    return PalaceGraphSnapshot.model_validate(payload)
