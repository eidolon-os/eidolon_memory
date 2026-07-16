"""Control-plane MCP tool factory (D1).

Each ``agent_runner`` process hosts its own FastMCP instance bound to the
loopback control-plane port. There is no longer a standalone MCP server entrypoint;
memory reads share the agent runner's ``LockedBackend`` while asynchronous
command-status reads use a separate SQLite projection and never contend on the
Chroma/KG lock.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from eidolon_sdk.memory import (
    KG_PREDICATE_VALUES,
    SENSITIVE_PREDICATES,
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryActorContext,
    MemoryIntent,
    MemoryIntentCommand,
    PrivacyMutationCommand,
)

from eidolon.memory.adapters.locked_kg import _now_iso
from eidolon.memory.application.forget import (
    ForgetResolutionLimitExceeded,
    find_forget_candidates,
)
from eidolon.memory.application.mempalace_hierarchy import build_mempalace_hierarchy_snapshot
from eidolon.memory.application.palace_graph import build_palace_graph
from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner
from eidolon.memory.application.privacy_filter import row_visible_to_listing
from eidolon.memory.application.public_recall import (
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import CommandStatusStore, DlqStore, MemoryBackend
from eidolon.memory.infrastructure.mempalace_backend import selected_mempalace_backend
from eidolon.memory.infrastructure.palace_init import palace_is_initialized
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def build_control_plane_mcp(
    backend: MemoryBackend,
    settings: MemorySettings,
    *,
    memory_space_id: str,
    palace_path: str,
    host: str,
    port: int,
    lifespan: Any = None,
    kg: Any = None,
    command_publisher: Any = None,
    command_status: CommandStatusStore | None = None,
    dlq_store: DlqStore | None = None,
    replay_publisher: Any = None,
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
        "json_response": cfg.json_response,
    }
    if lifespan is not None:
        mcp_kwargs["lifespan"] = lifespan

    mcp = FastMCP(f"eidolon-memory-{memory_space_id}", **mcp_kwargs)

    @mcp.tool()
    async def eidolon_memory_search(
        query: str,
        context: dict[str, Any],
        top_k: int = 5,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search this memory space using the supplied actor context."""
        ctx = MemoryActorContext.model_validate(context)
        records = await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            context=ctx,
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
        context: dict[str, Any],
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
        ctx = MemoryActorContext.model_validate(context)
        want_kg = settings.recall.kg_in_recall if include_kg is None else include_kg
        fused = await recall_with_kg_fusion(
            backend,
            settings,
            query=query,
            context=ctx,
            top_k=top_k,
            kg=kg if want_kg else None,
            for_voice=voice,
            palace_path=palace_path,
            include_sensitive_kg=include_sensitive_kg,
        )
        records = fused["vector"]
        kg_records = fused["kg"]
        wm_turns = fused.get("working_memory") or []
        return {
            "context": group_recall_context(
                records, kg_triples=kg_records, working_memory=wm_turns,
            ),
            "kg_triples": [t.model_dump(mode="json") for t in kg_records],
            "records": [wire_record_to_public_dict(r) for r in records],
            "working_memory": [t.model_dump(mode="json") for t in wm_turns],
        }

    @mcp.tool()
    async def eidolon_memory_status() -> dict[str, Any]:
        """Report this agent runner's memory service status."""
        mempalace_backend = selected_mempalace_backend(settings)
        initialized = palace_is_initialized(
            Path(palace_path),
            backend=mempalace_backend,
        )
        return {
            "backend": "mempalace-python",
            "mempalace_backend": mempalace_backend,
            "memory_space_id": memory_space_id,
            "palace_path": palace_path,
            "palace_initialized": initialized,
            "ready": initialized,
            "steward_mode": settings.steward.mode,
            "mcp_transport": "streamable-http",
            "mcp_http_url": settings.mcp_http.base_url(port=port),
            "wings": [w.model_dump() for w in settings.wings],
        }

    if command_status is not None:

        @mcp.tool()
        async def eidolon_memory_command_status(request_id: str) -> dict[str, Any]:
            """Read asynchronous write status without acquiring memory storage locks."""
            clean_id = (request_id or "").strip()
            if not clean_id:
                return {"status": "error", "error": "request_id is required"}
            record = await command_status.get(clean_id)
            if record is None:
                return {"status": "unknown", "request_id": clean_id}
            return record.to_dict()

        @mcp.tool()
        async def eidolon_memory_command_status_stats() -> dict[str, Any]:
            """Capacity and active-work metrics for the write-status projection."""
            return (await command_status.stats()).to_dict()

    if dlq_store is not None:
        _register_dlq_tools(
            mcp,
            dlq_store=dlq_store,
            replay_publisher=replay_publisher or command_publisher,
        )

    @mcp.tool()
    async def eidolon_memory_list(
        limit: int = 500,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Paginated listing of this memory space's drawers (Admin / IDE)."""
        lim = max(1, min(limit, 5000))
        off = max(0, offset)
        rows = await backend.get_all(memory_space_id, limit=lim, offset=off)
        filtered = [
            r for r in rows if row_visible_to_listing(r, include_private=include_private)
        ]
        return {
            "records": [wire_record_to_public_dict(r) for r in filtered],
            "total_hint": len(filtered),
        }

    @mcp.tool()
    async def eidolon_memory_get_by_source_turn(
        source_turn_id: str,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Exact drawer lookup by ``source_turn_id`` for sync/write probes."""
        turn_id = (source_turn_id or "").strip()
        if not turn_id:
            return {"record": None}
        row = await backend.get_by_source_turn_id(memory_space_id, turn_id)
        if row is None:
            return {"record": None}
        if not row_visible_to_listing(row, include_private=include_private):
            return {"record": None}
        return {"record": wire_record_to_public_dict(row)}

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

    if command_publisher is not None:
        _register_user_confirm_tool(
            mcp,
            command_publisher=command_publisher,
            memory_space_id=memory_space_id,
            command_status=command_status,
        )
        _register_privacy_tools(
            mcp,
            backend=backend,
            command_publisher=command_publisher,
            memory_space_id=memory_space_id,
            command_status=command_status,
        )

    if kg is not None and command_publisher is not None:
        _register_kg_tools(
            mcp,
            kg=kg,
            command_publisher=command_publisher,
            memory_space_id=memory_space_id,
            command_status=command_status,
        )

    return mcp


def _register_dlq_tools(
    mcp: Any,
    *,
    dlq_store: DlqStore,
    replay_publisher: Any,
) -> None:
    """Operational tools; raw payload bytes never cross the MCP boundary."""

    @mcp.tool()
    async def eidolon_memory_dlq_list(
        state: str = "unresolved",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List dead letters with bounded payload previews."""
        selected = None if state == "all" else state
        try:
            records = await dlq_store.list(
                state=selected,
                limit=max(1, min(limit, 500)),
                offset=max(0, offset),
            )
        except ValueError as exc:
            return {"status": "error", "error": str(exc), "records": []}
        return {
            "status": "ok",
            "records": [record.to_dict() for record in records],
            "stats": (await dlq_store.stats()).to_dict(),
        }

    @mcp.tool()
    async def eidolon_memory_dlq_detail(entry_id: str) -> dict[str, Any]:
        """Inspect one dead letter without exposing its full sensitive payload."""
        record = await dlq_store.get((entry_id or "").strip())
        if record is None:
            return {"status": "not_found", "entry_id": entry_id}
        return {"status": "ok", "record": record.to_dict()}

    @mcp.tool()
    async def eidolon_memory_dlq_replay(entry_id: str) -> dict[str, Any]:
        """Atomically claim and republish one unresolved original message."""
        clean_id = (entry_id or "").strip()
        item = await dlq_store.claim_replay(clean_id)
        if item is None:
            record = await dlq_store.get(clean_id)
            state = record.state if record is not None else "not_found"
            return {"status": "not_replayable", "entry_id": clean_id, "state": state}
        if replay_publisher is None:
            await dlq_store.release_replay(clean_id, error="replay publisher unavailable")
            return {"status": "failed", "entry_id": clean_id, "error": "replay unavailable"}
        try:
            await replay_publisher.replay_raw(item.record.subject, item.payload)
        except Exception as exc:  # noqa: BLE001 - return claim to unresolved
            record = await dlq_store.release_replay(clean_id, error=f"replay failed: {exc}")
            return {"status": "failed", "record": record.to_dict(), "error": str(exc)}
        record = await dlq_store.mark_replayed(clean_id)
        return {"status": "replayed", "record": record.to_dict()}

    @mcp.tool()
    async def eidolon_memory_dlq_resolve(entry_id: str, note: str) -> dict[str, Any]:
        """Resolve a dead letter without replay, retaining an operator note."""
        try:
            record = await dlq_store.resolve((entry_id or "").strip(), note=note)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "resolved", "record": record.to_dict()}


def _register_privacy_tools(
    mcp: Any,
    *,
    backend: MemoryBackend,
    command_publisher: Any,
    memory_space_id: str,
    command_status: CommandStatusStore | None,
) -> None:
    """Read-only preview followed by an exact-ID command on the write stream."""
    signer = PrivacyConfirmationSigner()

    @mcp.tool()
    async def eidolon_memory_forget_preview(
        target: str,
        action: str = "delete",
    ) -> dict[str, Any]:
        """Resolve a topic to exact drawers without changing memory.

        Physical deletion should be confirmed when this preview is ambiguous.
        The returned token binds the Realm, action and exact drawer IDs and
        expires after ten minutes.
        """
        clean_target = (target or "").strip()
        if not clean_target:
            return {"status": "error", "error": "target is required"}
        if action not in {"archive", "delete"}:
            return {"status": "error", "error": "action must be archive or delete"}
        try:
            candidates = await find_forget_candidates(
                backend, memory_space_id, clean_target
            )
        except ForgetResolutionLimitExceeded as exc:
            return {
                "status": "too_broad",
                "error": str(exc),
                "hint": "refine the target or preview an exact drawer_id",
            }
        if not candidates:
            return {"status": "not_found", "target": clean_target, "candidates": []}
        token, proof = signer.issue(
            memory_space_id=memory_space_id,
            action=action,  # type: ignore[arg-type]
            target=clean_target,
            drawer_ids=[candidate.key for candidate in candidates],
        )
        ambiguous = len(candidates) > 1 or any(candidate.score < 1.0 for candidate in candidates)
        return {
            "status": "preview",
            "preview_id": proof.preview_id,
            "target": clean_target,
            "action": action,
            "candidates": [candidate.to_dict() for candidate in candidates],
            "requires_explicit_confirmation": action == "delete" and ambiguous,
            "confirmation_token": token,
            "expires_at": proof.expires_at,
        }

    @mcp.tool()
    async def eidolon_memory_forget_confirm(
        confirmation_token: str,
        wait_applied_seconds: float = 0.75,
    ) -> dict[str, Any]:
        """Publish one previously previewed exact-ID archive/delete command."""
        try:
            proof = signer.verify(
                (confirmation_token or "").strip(),
                expected_memory_space_id=memory_space_id,
            )
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        command = PrivacyMutationCommand(
            request_id=uuid.uuid4().hex,
            memory_space_id=memory_space_id,
            issued_at=_now_iso(),
            issuer="agent",
            action=proof.action,
            drawer_ids=proof.drawer_ids,
            preview_id=proof.preview_id,
            target=proof.target,
        )
        outcome = await _publish_with_status(
            command_publisher,
            command_status,
            command,
            wait_seconds=wait_applied_seconds,
        )
        return {
            **outcome,
            "preview_id": proof.preview_id,
            "action": proof.action,
            "drawer_ids": proof.drawer_ids,
        }


async def _publish_with_status(
    command_publisher: Any,
    command_status: CommandStatusStore | None,
    command: Any,
    *,
    wait_seconds: float,
) -> dict[str, Any]:
    """Durably publish a write, then wait only on the lightweight projection."""
    try:
        await command_publisher.publish(command)
    except Exception as exc:  # noqa: BLE001 - surface a truthful tool outcome
        if command_status is not None:
            try:
                await command_status.record_failed(
                    command.request_id,
                    kind=command.kind,
                    error=f"publish failed: {exc}",
                )
            except Exception as status_exc:  # noqa: BLE001 - preserve root error
                log.error(
                    "command_status_publish_failure_record_failed",
                    request_id=command.request_id,
                    error=str(status_exc),
                )
        return {
            "status": "failed",
            "request_id": command.request_id,
            "error": f"publish failed: {exc}",
        }

    if command_status is None:
        return {"status": "accepted", "request_id": command.request_id}

    try:
        # The worker can win this race. Ledger transition rules guarantee a
        # late accepted update never downgrades applied/failed.
        await command_status.record_accepted(command.request_id, kind=command.kind)
        record = await command_status.wait_terminal(
            command.request_id,
            timeout_seconds=max(0.0, min(wait_seconds, 10.0)),
        )
    except Exception as exc:  # noqa: BLE001 - publish itself is already durable
        log.error(
            "command_status_read_failed",
            request_id=command.request_id,
            error=str(exc),
        )
        return {"status": "accepted", "request_id": command.request_id}

    if record is None:
        return {"status": "accepted", "request_id": command.request_id}
    return record.to_dict()


def _register_user_confirm_tool(
    mcp: Any,
    *,
    command_publisher: Any,
    memory_space_id: str,
    command_status: CommandStatusStore | None,
) -> None:
    """Phase 5.2 — verbatim-write tool, bypasses steward.

    Decoupled from ``_register_kg_tools`` because this writes a *fragment*
    (chromadb drawer), not a KG triple. Only needs ``command_publisher`` —
    no ``kg`` dependency.
    """

    @mcp.tool()
    async def eidolon_memory_user_confirm(
        text: str,
        wing: str = "Wing_Profile",
        memory_type: str = "profile",
        importance: int = 5,
        confidence: float = 0.99,
        tags: list[str] | None = None,
        scope: str = "persona",
        visibility: str = "all_devices",
        source_device_id: str = "",
        target_device_id: str | None = None,
        source_instance_id: str = "",
        session_id: str = "",
        extensions: dict[str, dict[str, Any]] | None = None,
        source_event_id: str = "",
        tool_call_id: str = "",
        wait_applied_seconds: float = 0.75,
    ) -> dict[str, Any]:
        """Persist a user-confirmed fact verbatim, bypassing the LLM steward.

        Use when the caller (LiveKit voice agent / IDE / chat UI) has
        positively determined the user wants something remembered
        word-for-word — e.g. "记住我喝乌龙茶不喝绿茶". The steward path
        (where LLM may paraphrase, mis-route, or drop the statement
        entirely) is **not** appropriate for this intent.

        Writes:
          - ``metadata.source = "user-confirmed"`` — recall pins these
            ahead of cosine-ranked drawers in the same wing.
          - ``confidence = 0.99`` (caller-override allowed) — the
            highest-trust signal in the system short of KG facts.
          - ``importance = 5`` default — explicit user intent ranks
            top of the importance ladder.

        Returns a truthful ``accepted``/``applied``/``failed`` status. Idempotency:
        re-publishing the same ``request_id`` (caller can't drive that
        from the tool, but JetStream redelivery does) collapses at chroma.
        """
        clean = (text or "").strip()
        if not clean:
            return {
                "status": "error",
                "error": "text must be a non-empty string",
            }
        request_id = uuid.uuid4().hex
        event_id = source_event_id.strip() or request_id
        intent = MemoryIntent(
            intent_id=f"intent:{request_id}",
            memory_space_id=memory_space_id,
            source_event_id=event_id,
            authority="explicit_user",
            intent_type=(
                "preference" if memory_type.strip().lower() == "preference" else "fact"
            ),
            raw_claim=clean,
            operation_hint="confirm",
            occurred_at=_now_iso(),
            tool_call_id=tool_call_id.strip() or None,
            confidence=max(0.0, min(1.0, confidence)),
            attributes={
                "wing": wing,
                "memory_type": memory_type,
                "importance": max(1, min(5, importance)),
                "tags": list(tags or []),
                "scope": scope,
                "visibility": visibility,
                "source_device_id": source_device_id,
                "target_device_id": target_device_id,
                "source_instance_id": source_instance_id,
                "session_id": session_id,
                "extensions": dict(extensions or {}),
            },
        )
        cmd = MemoryIntentCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
            issued_at=_now_iso(),
            issuer="agent",
            intent=intent,
        )
        outcome = await _publish_with_status(
            command_publisher,
            command_status,
            cmd,
            wait_seconds=wait_applied_seconds,
        )
        return {
            **outcome,
            "wing": wing,
            "intent_id": intent.intent_id,
            "source_event_id": event_id,
        }


# palace_graph business logic lives in eidolon.memory.application.palace_graph
# (thin shell here keeps entrypoints layer pure).


def _register_kg_tools(
    mcp: Any,
    *,
    kg: Any,
    command_publisher: Any,
    memory_space_id: str,
    command_status: CommandStatusStore | None,
) -> None:
    """Register the 6 KG tools on the FastMCP instance (KG plan §3.3).

    Write tools publish to NATS and wait on the separate command-status
    projection; read tools query the LockedKnowledgeGraph directly. A legacy
    storage-polling fallback remains only for embedders that omit the ledger.
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
        """Queue a temporal triple write via NATS; wait for worker status (≤2s).

        All admin writes share the same JetStream pipeline as chat turns.
        Replay can recover edits only inside configured JetStream retention;
        it is not a multi-year source of truth.
        """
        request_id = uuid.uuid4().hex
        cmd = KgAddTripleCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
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
        if command_status is not None:
            outcome = await _publish_with_status(
                command_publisher,
                command_status,
                cmd,
                wait_seconds=wait_visible_seconds,
            )
            return {
                **outcome,
                "triple_id": outcome.get("resource_id"),
            }

        await command_publisher.publish(cmd)
        # Compatibility path for embedders that have not supplied the
        # read-optimized status projection.
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
            memory_space_id=memory_space_id,
            issued_at=_now_iso(),
            subject=subject,
            predicate=predicate,
            object=object,
            ended=ended_iso,
        )
        if command_status is not None:
            return await _publish_with_status(
                command_publisher,
                command_status,
                cmd,
                wait_seconds=wait_visible_seconds,
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
