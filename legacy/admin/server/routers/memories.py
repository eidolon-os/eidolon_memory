"""Memory HTTP: reads via per-user MCP; writes via per-user JetStream subject."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from dependencies import AdminAuth, SettingsDep, TurnPublisherDep
from fastapi import APIRouter, HTTPException, Query
from mcp_call import call_user_mcp
from schemas import MemoryCreateRequest, MemoryListResponse, MemorySearchResponse
from user_registry import resolve_user_entry

from eidolon.memory.domain.payloads import ConversationTurnPayload

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("/search", response_model=MemorySearchResponse)
async def search_memories(
    _: AdminAuth,
    settings: SettingsDep,
    query: str = Query(..., min_length=1),
    user_id: str = Query(..., description="users.yaml agent id"),
    top_k: int = Query(5, ge=1, le=100),
    wing: str | None = None,
    room: str | None = None,
) -> MemorySearchResponse:
    args: dict[str, object] = {
        "query": query,
        "top_k": top_k,
        "wing": wing,
        "room": room,
    }
    payload = await call_user_mcp(settings, user_id, "eidolon_memory_search", args)
    if not isinstance(payload, list):
        raise HTTPException(status_code=502, detail="unexpected MCP search payload")
    return MemorySearchResponse(records=payload)


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    _: AdminAuth,
    settings: SettingsDep,
    user_id: str = Query(..., description="users.yaml agent id"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    include_private: bool = Query(False),
) -> MemoryListResponse:
    args = {
        "limit": limit,
        "offset": offset,
        "include_private": include_private,
    }
    payload = await call_user_mcp(settings, user_id, "eidolon_memory_list", args)
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
    uid = body.user_id.strip()
    if not uid:
        raise HTTPException(status_code=400, detail="user_id is required")
    resolve_user_entry(settings, uid)

    meta = dict(body.metadata or {})
    meta.setdefault("source", "eidolon-memory-admin")
    meta.setdefault("wing", body.wing.strip())
    meta.setdefault("room", body.room.strip())

    payload = ConversationTurnPayload(
        turn_id=str(uuid.uuid4()),
        user_text=body.text,
        assistant_text="",
        timestamp=datetime.now(UTC).replace(microsecond=0).isoformat(),
        session_id=body.room.strip(),
        user_id=uid,
        metadata=meta,
    )
    try:
        await publisher.publish_turn(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    detail = (
        f"Published to agent.memory.conversation.turn.{uid}; "
        "agent_runner will steward + persist in-process."
    )
    if settings.steward.mode.strip().lower() != "noop":
        detail += " Non-noop steward may restructure fragments rather than store verbatim text."
    return {"status": "accepted", "detail": detail}


@router.delete("/{key}")
async def delete_memory(
    _: AdminAuth,
    key: str,
    user_id: str = Query("", description="Ignored; delete removed in D1 control-plane."),
) -> dict[str, str]:
    del user_id
    raise HTTPException(
        status_code=501,
        detail=(
            "Delete is not available on the D1 control-plane MCP. "
            "Remove drawers via MemPalace tooling or a future admin write path."
        ),
    )
