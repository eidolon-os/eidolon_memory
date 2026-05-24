"""Control-plane MCP tool factory (D1).

Each ``agent_runner`` process hosts its own FastMCP instance bound to the
loopback control-plane port. There is no longer a standalone MCP server entrypoint;
tools share the agent runner's ``LockedBackend`` (single PersistentClient per palace,
single ``asyncio.Lock`` for read+write).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from eidolon.memory.adapters.locked_kg import _now_iso
from eidolon.memory.application.mempalace_hierarchy import build_mempalace_hierarchy_snapshot
from eidolon.memory.application.palace_graph import build_palace_graph
from eidolon.memory.application.privacy_filter import row_visible_to_listing
from eidolon.memory.application.public_recall import (
    group_recall_context,
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.kg import (
    KG_PREDICATE_VALUES,
    SENSITIVE_PREDICATES,
    KgAddTripleCommand,
    KgInvalidateCommand,
)
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def build_control_plane_mcp(
    backend: MemoryBackend,
    settings: MemorySettings,
    *,
    user_id: str,
    palace_path: str,
    host: str,
    port: int,
    lifespan: Any = None,
    kg: Any = None,
    command_publisher: Any = None,
):
    """Construct a FastMCP server bound to ``(host, port)`` for one user's runner.

    Tools run in-process against the supplied ``backend`` (typically a
    ``LockedBackend`` wrapping the user's ``MemPalacePythonBackend``). The
    same instance is also called directly by ``LiveKitRecallService`` — both
    paths share the lock.

    ``kg`` is a per-runner :class:`LockedKnowledgeGraph` (read-side: direct;
    write-side: via ``command_publisher`` → NATS, per KG plan §3.3). When
    ``kg``/``command_publisher`` are absent the KG tools are not registered —
    keeps the tool surface clean for backwards-compatible smoke tests.
    """
    from mcp.server.fastmcp import FastMCP

    cfg = settings.mcp_http
    streamable_path = cfg.path if cfg.path.startswith("/") else f"/{cfg.path}"

    mcp_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "streamable_http_path": streamable_path,
        "stateless_http": cfg.stateless_http,
    }
    if lifespan is not None:
        mcp_kwargs["lifespan"] = lifespan

    mcp = FastMCP(f"eidolon-memory-{user_id}", **mcp_kwargs)

    @mcp.tool()
    async def eidolon_memory_search(
        query: str,
        top_k: int = 5,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search this user's memory; ``user_id`` is bound by the agent runner."""
        records = await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            wing=wing,
            room=room,
            for_voice=False,
            palace_path=palace_path,
        )
        return [wire_record_to_public_dict(r) for r in records]

    @mcp.tool()
    async def eidolon_memory_recall_context(
        query: str,
        top_k: int = 5,
        voice: bool = False,
        include_kg: bool | None = None,
        include_sensitive_kg: bool = False,
    ) -> dict[str, Any]:
        """Aggregated recall: vector + (optional) KG triples in parallel.

        ``voice=True`` enables the LiveKit hot-path optimizations
        (shared query embedding across wings, skip closets); the LiveKit
        pipeline calls the same code via ``LiveKitRecallService.recall_context``.
        ``include_kg`` defaults to settings.recall.kg_in_recall.
        ``include_sensitive_kg`` opt-in for health predicates.
        """
        want_kg = settings.recall.kg_in_recall if include_kg is None else include_kg
        fused = await recall_with_kg_fusion(
            backend,
            settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            kg=kg if want_kg else None,
            for_voice=voice,
            palace_path=palace_path,
            include_sensitive_kg=include_sensitive_kg,
        )
        records = fused["vector"]
        kg_records = fused["kg"]
        return {
            "context": group_recall_context(records, kg_triples=kg_records),
            "kg_triples": [t.model_dump(mode="json") for t in kg_records],
            "records": [wire_record_to_public_dict(r) for r in records],
        }

    @mcp.tool()
    async def eidolon_memory_status() -> dict[str, Any]:
        """Report this agent runner's memory service status."""
        return {
            "backend": "mempalace-python",
            "user_id": user_id,
            "palace_path": palace_path,
            "steward_mode": settings.steward.mode,
            "mcp_transport": "streamable-http",
            "mcp_http_url": settings.mcp_http.base_url(port=port),
            "wings": [w.model_dump() for w in settings.wings],
        }

    @mcp.tool()
    async def eidolon_memory_list(
        limit: int = 500,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Paginated listing of this user's drawers (Admin / IDE)."""
        lim = max(1, min(limit, 5000))
        off = max(0, offset)
        rows = await backend.get_all(user_id, limit=lim, offset=off)
        filtered = [
            r for r in rows if row_visible_to_listing(r, include_private=include_private)
        ]
        return {
            "records": [wire_record_to_public_dict(r) for r in filtered],
            "total_hint": len(filtered),
        }

    @mcp.tool()
    async def eidolon_memory_hierarchy_snapshot(
        max_records: int = 8000,
        max_drawers_per_room: int = 48,
    ) -> dict[str, Any]:
        """Return wing→room→drawer tree snapshot (bounded scan)."""
        mr = max(50, min(max_records, 50_000))
        md = max(4, min(max_drawers_per_room, 400))
        return await build_mempalace_hierarchy_snapshot(
            backend,
            settings,
            palace_path=palace_path,
            max_records=mr,
            max_drawers_per_room=md,
        )

    @mcp.tool()
    async def eidolon_memory_palace_graph(
        max_nodes: int = 120,
        max_edges: int = 200,
    ) -> dict[str, Any]:
        """Cross-wing tunnel graph (rooms shared between wings).

        Built inside the agent_runner so the chromadb PersistentClient stays
        single-owner (D1). Admin must call this MCP tool rather than open a
        second chromadb handle on the same palace.
        """
        mn = max(10, min(max_nodes, 800))
        me = max(10, min(max_edges, 2000))
        return await build_palace_graph(
            backend,
            palace_path=palace_path,
            max_nodes=mn,
            max_edges=me,
        )

    if kg is not None and command_publisher is not None:
        _register_kg_tools(
            mcp, kg=kg, command_publisher=command_publisher, user_id=user_id
        )

    return mcp


# palace_graph business logic lives in eidolon.memory.application.palace_graph
# (thin shell here keeps entrypoints layer pure).


def _register_kg_tools(mcp: Any, *, kg: Any, command_publisher: Any, user_id: str) -> None:
    """Register the 6 KG tools on the FastMCP instance (KG plan §3.3).

    Write tools publish to NATS (sync-feel polling for visibility); read tools
    query the LockedKnowledgeGraph directly.
    """
    @mcp.tool()
    async def eidolon_memory_kg_add_triple(
        subject: str,
        predicate: str,
        object: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
        wait_visible_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Queue a temporal triple write via NATS; polls until visible (≤2s).

        All admin writes share the same JetStream pipeline as chat turns so
        rebuild-from-replay naturally recovers admin edits (D5).
        """
        request_id = uuid.uuid4().hex
        cmd = KgAddTripleCommand(
            request_id=request_id,
            user_id=user_id,
            issued_at=_now_iso(),
            subject=subject,
            predicate=predicate,
            object=object,
            valid_from=valid_from,
            valid_to=valid_to,
            confidence=confidence,
            source_drawer_id=f"req:{request_id}",
            adapter_name="admin",
        )
        await command_publisher.publish(cmd)
        # Sync-feel polling
        deadline = time.monotonic() + wait_visible_seconds
        while time.monotonic() < deadline:
            tid = await kg.find_pending_triple_id(
                f"req:{request_id}", subject, predicate, object
            )
            if tid:
                return {
                    "status": "applied",
                    "request_id": request_id,
                    "triple_id": tid,
                }
            await asyncio.sleep(0.03)
        return {"status": "pending", "request_id": request_id, "triple_id": None}

    @mcp.tool()
    async def eidolon_memory_kg_invalidate(
        subject: str,
        predicate: str,
        object: str,
        ended: str | None = None,
        wait_visible_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Mark a triple ended via NATS. Returns when worker has applied it."""
        request_id = uuid.uuid4().hex
        ended_iso = ended or _now_iso()
        cmd = KgInvalidateCommand(
            request_id=request_id,
            user_id=user_id,
            issued_at=_now_iso(),
            subject=subject,
            predicate=predicate,
            object=object,
            ended=ended_iso,
        )
        await command_publisher.publish(cmd)
        deadline = time.monotonic() + wait_visible_seconds
        while time.monotonic() < deadline:
            applied = await kg.find_invalidation_applied(
                subject, predicate, object, ended_iso
            )
            if applied:
                return {"status": "applied", "request_id": request_id}
            await asyncio.sleep(0.03)
        return {"status": "pending", "request_id": request_id}

    @mcp.tool()
    async def eidolon_memory_kg_query_entity(
        name: str,
        as_of: str | None = None,
        direction: str = "outgoing",
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Return triples linked to entity ``name`` at point ``as_of`` (default NOW).

        Sensitive predicates (health, medication) are excluded unless
        ``include_sensitive`` is True.
        """
        records = await kg.query_entity(
            name,
            as_of=as_of,
            direction=direction,
            include_sensitive=include_sensitive,
        )
        return {
            "entity": name,
            "as_of": as_of or _now_iso(),
            "direction": direction,
            "triples": [r.model_dump(mode="json") for r in records],
        }

    @mcp.tool()
    async def eidolon_memory_kg_timeline(
        entity_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Chronological events; entity-scoped if name given, else global."""
        records = await kg.timeline(
            entity_name=entity_name,
            since=since,
            until=until,
            limit=limit,
            include_sensitive=include_sensitive,
        )
        return {
            "entity_name": entity_name,
            "since": since,
            "until": until,
            "events": [r.model_dump(mode="json") for r in records],
        }

    @mcp.tool()
    async def eidolon_memory_kg_stats() -> dict[str, Any]:
        """Entity/triple counts + active/invalidated split."""
        return await kg.stats()

    @mcp.tool()
    async def eidolon_memory_kg_snapshot(
        max_triples: int = 400,
        current_only: bool = True,
        entity: str | None = None,
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Bounded triple snapshot for graph visualization.

        One round-trip returning ``stats`` + a capped triple list — wraps
        :meth:`LockedKnowledgeGraph.timeline` (which already runs under the
        backend lock and filters sensitive predicates).
        """
        limit = max(10, min(max_triples, 5000))
        records = await kg.timeline(
            entity_name=entity if entity else None,
            limit=limit,
            include_sensitive=include_sensitive,
        )
        if current_only:
            records = [r for r in records if r.valid_to is None]
        s = await kg.stats()
        return {
            "stats": s,
            "triples": [r.model_dump(mode="json") for r in records],
            "capped": len(records) >= limit,
            "triple_count": len(records),
        }

    @mcp.tool()
    async def eidolon_memory_kg_predicates() -> dict[str, Any]:
        """Whitelist of canonical predicates; admin clients introspect schema."""
        return {
            "predicates": list(KG_PREDICATE_VALUES),
            "sensitive": sorted(SENSITIVE_PREDICATES),
            "count": len(KG_PREDICATE_VALUES),
        }
