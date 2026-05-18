"""Memory HTTP surface: reads via MCP tools, writes via JetStream (worker)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from dependencies import AdminAuth, McpSessionDep, SettingsDep, TurnPublisherDep
from fastapi import APIRouter, HTTPException, Query
from schemas import MemoryCreateRequest, MemoryListResponse, MemorySearchResponse

from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.infrastructure.mcp_http_client import call_tool_json

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("/search", response_model=MemorySearchResponse)
async def search_memories(
    _: AdminAuth,
    mcp: McpSessionDep,
    query: str = Query(..., min_length=1),
    user_id: str = Query("default"),
    top_k: int = Query(5, ge=1, le=100),
    wing: str | None = None,
    room: str | None = None,
) -> MemorySearchResponse:
    args: dict[str, object] = {
        "query": query,
        "user_id": user_id,
        "top_k": top_k,
        "wing": wing,
        "room": room,
    }
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_search", args)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not isinstance(payload, list):
        raise HTTPException(status_code=502, detail="unexpected MCP search payload")
    return MemorySearchResponse(records=payload)


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    _: AdminAuth,
    mcp: McpSessionDep,
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
    args = {
        "tenant_id": tid,
        "limit": limit,
        "offset": offset,
        "include_private": include_private,
    }
    try:
        payload = await call_tool_json(mcp, "eidolon_memory_list", args)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="unexpected MCP list payload")
    records = payload.get("records")
    hint = payload.get("total_hint")
    if not isinstance(records, list):
        raise HTTPException(status_code=502, detail="invalid MCP list records")
    return MemoryListResponse(
        records=records,
        total_hint=int(hint) if isinstance(hint, int) else len(records),
    )


@router.post("", status_code=202)
async def create_memory(
    _: AdminAuth,
    publisher: TurnPublisherDep,
    settings: SettingsDep,
    body: MemoryCreateRequest,
) -> dict[str, str]:
    meta = dict(body.metadata or {})
    meta.setdefault("source", "eidolon-memory-admin")
    payload = ConversationTurnPayload(
        turn_id=str(uuid.uuid4()),
        user_text=body.text,
        assistant_text="",
        timestamp=datetime.now(UTC).replace(microsecond=0).isoformat(),
        session_id=body.room,
        user_id=body.wing,
        metadata=meta,
    )
    try:
        await publisher.publish_turn(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    detail = "JetStream envelope published; memory worker will persist."
    if settings.steward.mode.strip().lower() != "noop":
        detail += (
            " Steward is not noop: the worker may extract/restructure fragments rather than "
            "store this text verbatim."
        )
    return {"status": "accepted", "detail": detail}


@router.delete("/{key}")
async def delete_memory(
    _: AdminAuth,
    mcp: McpSessionDep,
    key: str,
    user_id: str = Query("", description="Ignored; kept for backwards-compatible query URLs."),
) -> dict[str, str]:
    del user_id
    if not key.startswith("drawer_"):
        raise HTTPException(status_code=400, detail="key must be a MemPalace drawer_* id")
    try:
        await call_tool_json(mcp, "eidolon_memory_delete", {"key": key})
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "deleted", "key": key}
