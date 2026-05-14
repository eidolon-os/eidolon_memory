"""Memory CRUD-ish HTTP surface (search matches MCP semantics)."""

from __future__ import annotations

from dependencies import AdminAuth, BackendDep, SettingsDep, get_serialize_lock
from fastapi import APIRouter, HTTPException, Query
from schemas import MemoryCreateRequest, MemoryListResponse, MemorySearchResponse

from eidolon.memory.application.ingest import ingest_fragment
from eidolon.memory.application.public_recall import (
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.domain.wire import MemoryWireRecord

router = APIRouter(prefix="/memories", tags=["memories"])


def _admin_row_visible(rec: MemoryWireRecord, *, include_private: bool) -> bool:
    if include_private:
        return True
    if rec.metadata.get("wing") == "Wing_Privacy" or rec.user_id == "Wing_Privacy":
        return False
    return True


@router.get("/search", response_model=MemorySearchResponse)
async def search_memories(
    _: AdminAuth,
    backend: BackendDep,
    settings: SettingsDep,
    query: str = Query(..., min_length=1),
    user_id: str = Query("default"),
    top_k: int = Query(5, ge=1, le=100),
    wing: str | None = None,
    room: str | None = None,
) -> MemorySearchResponse:
    records = await search_all_wings_mcp_style(
        backend,
        settings,
        query=query,
        user_id=user_id,
        top_k=top_k,
        wing=wing,
        room=room,
    )
    return MemorySearchResponse(records=[wire_record_to_public_dict(r) for r in records])


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    _: AdminAuth,
    backend: BackendDep,
    tenant_id: str | None = Query(
        None,
        description=(
            "Omit or leave empty to paginate across all drawers in this palace. "
            "Otherwise filter rows whose metadata matches this ``user_id`` or ``wing``."
        ),
    ),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    include_private: bool = Query(False),
) -> MemoryListResponse:
    tid = (tenant_id or "").strip()
    rows = await backend.get_all(tid, limit=limit, offset=offset)
    filtered = [r for r in rows if _admin_row_visible(r, include_private=include_private)]
    return MemoryListResponse(
        records=[wire_record_to_public_dict(r) for r in filtered],
        total_hint=len(filtered),
    )


@router.post("", status_code=201)
async def create_memory(
    _: AdminAuth,
    backend: BackendDep,
    body: MemoryCreateRequest,
) -> dict[str, str]:
    meta = dict(body.metadata or {})
    try:
        await ingest_fragment(
            backend,
            wing=body.wing,
            room=body.room,
            text=body.text,
            metadata=meta,
            serialize_lock=get_serialize_lock(),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@router.delete("/{key}")
async def delete_memory(
    _: AdminAuth,
    backend: BackendDep,
    key: str,
    user_id: str = Query("", description="Reserved for backends that scope delete by tenant."),
) -> dict[str, str]:
    del user_id
    if not key.startswith("drawer_"):
        raise HTTPException(status_code=400, detail="key must be a MemPalace drawer_* id")
    try:
        await backend.delete("", key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted", "key": key}
