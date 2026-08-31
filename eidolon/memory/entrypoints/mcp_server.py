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
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from eidolon_memory_contracts import (
    KG_PREDICATE_VALUES,
    SENSITIVE_PREDICATES,
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryActorContext,
    PrivacyMutationCommand,
    RecallPlan,
)

from eidolon.memory.adapters.fixed_space_router import FixedSpaceRouter
from eidolon.memory.adapters.kg_sqlite import now_iso as _now_iso
from eidolon.memory.application.command_delivery import publish_with_status
from eidolon.memory.application.forget import (
    ForgetResolutionLimitExceeded,
    find_forget_candidates,
)
from eidolon.memory.application.memory_service import MemoryService
from eidolon.memory.application.mempalace_hierarchy import build_mempalace_hierarchy_snapshot
from eidolon.memory.application.palace_graph import build_palace_graph
from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner
from eidolon.memory.application.privacy_filter import row_visible_to_listing
from eidolon.memory.application.public_recall import (
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import (
    CanonicalFactReader,
    CommandStatusStore,
    CommitmentReader,
    DlqStore,
    MemoryBackend,
)
from eidolon.memory.domain.predicates import predicate_definition
from eidolon.memory.domain.space_runtime import MemorySpaceRuntime, SpaceLedgers
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


#: Who a tool is for. Declared at each tool rather than derived from a list
#: elsewhere, so adding one forces the question — and getting it wrong by putting an
#: operator tool on the agent surface is the mistake with consequences.
_AGENT = "agent"
_OPS = "ops"

#: The narrow read tools the conversational agent calls. Everything else on this server is
#: for operators, benchmarks and the admin UI.
#:
#: Measured before splitting: 27 tools were 15,602 characters of name, description
#: and JSON schema — about 3,900 tokens in front of every agent request, of which
#: 3,347 described tools the agent must never call. That is the smaller half of the
#: problem. The larger half is that the list included ``forget_confirm``,
#: ``dlq_replay``, ``dlq_resolve`` and ``kg_invalidate``: a model
#: reading "忘了这件事吧" from a user had a plausible destructive tool in reach, and
#: nothing but its own judgement between the two.
AGENT_SURFACE_TOOLS = (
    "eidolon_memory_search",
    "eidolon_memory_recall_context",
    "eidolon_memory_active_commitments",
)


def _audience_gate(mcp: Any, surface: str):
    """Return a decorator that registers a tool only if this surface serves it.

    ``surface="agent"`` keeps the two the agent calls. ``surface="all"`` keeps
    everything, and is the default so every existing caller — tests, the admin
    surface, the benches — is unaffected by the split.

    There is no ``"ops"`` surface with the agent tools removed. An operator
    debugging a space wants ``search`` more than anything else on the list, and a
    second endpoint that could not answer the first question anyone asks would just
    be answered by opening the agent's.
    """

    def tool(audience: str):
        def decorate(fn):
            if surface == "all" or audience == _AGENT:
                mcp.tool()(fn)
            return fn

        return decorate

    return tool


def _actor_context_for_surface(
    context: dict[str, Any],
    *,
    surface: str,
) -> MemoryActorContext:
    """Validate actor context at the trust boundary that serves it.

    Council storage remains available to the operator surface for contract and
    projection work, but the conversational Agent surface has no authoritative
    participant-scope adapter yet. Accepting a bare ``council_id`` there would
    let a caller mint its own audience. Keep the product feature closed until
    that authority exists instead of treating a string as proof.
    """

    ctx = MemoryActorContext.model_validate(context)
    if surface == "agent" and ctx.council_id:
        raise ValueError(
            "Council memory requires an authoritative participant-scope adapter; "
            "bare council_id is not accepted on the Agent surface"
        )
    return ctx


async def _all_audiences(kg: Any) -> tuple[str, ...]:
    """Every audience a space's graph actually contains.

    Operator tools inspect one space in full rather than through one companion's
    view. Enumerating what is there beats a wildcard: a "match any audience"
    token would be a way past the filter, and the filter is the only thing
    keeping one companion's private statements out of another's recall.

    Sensitivity is gated separately by ``include_sensitive`` — seeing every
    audience is not the same as seeing every predicate.
    """

    return tuple(await kg.known_audiences())


def build_control_plane_mcp(
    backend: MemoryBackend,
    settings: MemorySettings,
    *,
    service: MemoryService | None = None,
    memory_space_id: str,
    palace_path: str,
    host: str,
    port: int,
    lifespan: Any = None,
    kg: Any = None,
    command_publisher: Any = None,
    command_status: CommandStatusStore | None = None,
    canonical_facts: CanonicalFactReader | None = None,
    commitments: CommitmentReader | None = None,
    dlq_store: DlqStore | None = None,
    replay_publisher: Any = None,
    surface: str = "all",
    path: str | None = None,
):
    """Construct a FastMCP server bound to ``(host, port)`` for one user's runner.

    Tools run in-process against the supplied ``backend`` (typically a
    ``LockedBackend`` wrapping the user's ``MemPalacePythonBackend``). The
    same instance is also called directly by ``LiveKitRecallService`` — both
    paths share the lock.

    ``kg`` is a per-runner a :class:`KnowledgeGraphPort` (read-side: direct;
    write-side: via ``command_publisher`` → NATS, per KG plan §3.3). When
    ``kg``/``command_publisher`` are absent the KG tools are not registered —
    keeps the tool surface clean for backwards-compatible smoke tests.

    ``surface`` selects who the server is for: ``"agent"`` registers only
    :data:`AGENT_SURFACE_TOOLS`, ``"all"`` registers everything. Defaults to
    ``"all"`` so nothing that already calls this changes. ``path`` overrides the
    streamable-HTTP path, which is how two of these live on one port.
    """
    from mcp.server.fastmcp import FastMCP

    cfg = settings.mcp_http
    raw_path = cfg.path if path is None else path
    streamable_path = raw_path if raw_path.startswith("/") else f"/{raw_path}"

    mcp_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "streamable_http_path": streamable_path,
        "stateless_http": cfg.stateless_http,
        "json_response": cfg.json_response,
    }
    if lifespan is not None:
        mcp_kwargs["lifespan"] = lifespan

    if service is None:
        # A caller that already resolved its one space can hand the handles over
        # and skip building a router. The read tools still go through the service,
        # so there is one code path rather than two — this router just answers
        # with what it was given, and refuses any other space.
        service = MemoryService(
            FixedSpaceRouter(
                MemorySpaceRuntime(
                    space_id=memory_space_id,
                    backend=backend,
                    palace_path=palace_path,
                    kg=kg,
                    ledgers=SpaceLedgers(
                        command_status=command_status,
                        dlq=dlq_store,
                        canonical_facts=canonical_facts,
                        commitments=commitments,
                    ),
                )
            ),
            settings,
            command_publisher=command_publisher,
        )

    name = f"eidolon-memory-{memory_space_id}"
    mcp = FastMCP(name if surface == "all" else f"{name}-{surface}", **mcp_kwargs)
    tool = _audience_gate(mcp, surface)

    @tool(_AGENT)
    async def eidolon_memory_search(
        query: str,
        context: dict[str, Any],
        top_k: int = 5,
        wing: str | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search the caller's memory space.

        Which space that is comes from ``context``, not from this process — that
        is the only thing this tool changed. Search stays a lookup: no graph
        fusion, no recent turns, no conversational filtering. Those belong to
        recall_context, which answers a different question — "what is relevant to
        this turn" rather than "what do you remember about this".
        """
        ctx = _actor_context_for_surface(context, surface=surface)
        runtime = await service.runtime_for(ctx)
        records = await search_all_wings_mcp_style(
            runtime.backend,
            settings,
            query=query,
            context=ctx,
            top_k=top_k,
            wing=wing,
            room=room,
            for_voice=False,
            palace_path=runtime.palace_path,
        )
        return [wire_record_to_public_dict(r) for r in records]

    @tool(_AGENT)
    async def eidolon_memory_recall_context(
        query: str,
        context: dict[str, Any],
        top_k: int = 5,
        voice: bool = False,
        include_kg: bool | None = None,
        kg_subjects: list[str] | None = None,
    ) -> dict[str, Any]:
        """Aggregated recall: vector + (optional) KG triples in parallel.

        ``voice=True`` enables the LiveKit hot-path optimizations
        (shared query embedding across wings, skip closets); the LiveKit
        pipeline calls the same code via ``LiveKitRecallService.recall_context``.
        ``include_kg`` defaults to settings.recall.kg_in_recall.

        There is deliberately no ``include_sensitive_kg`` parameter here. Health
        predicates are widened by ``recall.include_sensitive_kg`` in settings, a
        deployment decision, not by an argument the caller supplies per request.
        Audience already works that way — the agent passes ``context`` and the
        visible set is *derived* from it — and sensitivity is the same kind of
        thing: a capability, which the least-trusted caller should not be able to
        grant itself. The operator surface keeps the explicit parameter, where
        turning it on is a human act.
        """
        ctx = _actor_context_for_surface(context, surface=surface)
        subjects = tuple(
            value.strip()
            for value in (kg_subjects or [])
            if isinstance(value, str) and value.strip()
        )
        fused = await service.recall_fused(
            ctx,
            query,
            plan=RecallPlan(semantic_k=top_k, voice=voice, focus_subjects=subjects),
            include_sensitive_kg=settings.recall.include_sensitive_kg,
            include_kg=include_kg,
        )
        return {
            "context": fused["context"],
            "kg_triples": fused["kg_triples"],
            "records": fused["records"],
            "working_memory": fused["working_memory"],
            "degraded": fused["degraded"],
            "degraded_reason": fused["degraded_reason"],
            "trace": fused["trace"],
        }

    @tool(_OPS)
    async def eidolon_memory_status() -> dict[str, Any]:
        """Report storage readability and projection convergence."""
        try:
            mempalace_version = version("mempalace")
        except PackageNotFoundError:
            mempalace_version = "unknown"
        materialization = await service.status(
            MemoryActorContext(
                memory_realm_id=memory_space_id,
                memory_space_id=memory_space_id,
            )
        )
        return {
            "backend": "mempalace-python",
            "mempalace_version": mempalace_version,
            "mempalace_backend": settings.mempalace.backend,
            "memory_space_id": memory_space_id,
            "palace_path": palace_path,
            "palace_initialized": materialization.details.get("data_readable", False),
            "ready": materialization.ready,
            **materialization.details,
            "steward_mode": settings.steward.mode,
            "mcp_transport": "streamable-http",
            "mcp_http_url": settings.mcp_http.base_url(port=port),
            "wings": [w.model_dump() for w in settings.wings],
        }

    if command_status is not None:

        @tool(_OPS)
        async def eidolon_memory_command_status(request_id: str) -> dict[str, Any]:
            """Read asynchronous write status without acquiring memory storage locks."""
            clean_id = (request_id or "").strip()
            if not clean_id:
                return {"status": "error", "error": "request_id is required"}
            record = await command_status.get(clean_id)
            if record is None:
                return {"status": "unknown", "request_id": clean_id}
            return record.to_dict()

        @tool(_OPS)
        async def eidolon_memory_command_status_stats() -> dict[str, Any]:
            """Capacity and active-work metrics for the write-status projection."""
            return (await command_status.stats()).to_dict()

    if canonical_facts is not None:

        @tool(_OPS)
        async def eidolon_memory_canonical_stats() -> dict[str, Any]:
            """Read exact-fact evidence and projection-state counts."""
            return (await canonical_facts.stats()).to_dict()

        @tool(_OPS)
        async def eidolon_memory_fact_history(
            subject: str,
            predicate: str,
            object_value: str | None = None,
            limit: int = 100,
            include_sensitive: bool = False,
        ) -> dict[str, Any]:
            """Current state and auditable lifecycle for one canonical fact slot."""
            clean_subject = (subject or "").strip()
            clean_predicate = (predicate or "").strip()
            clean_object = (object_value or "").strip() or None
            if not clean_subject or not clean_predicate:
                return {"status": "error", "error": "subject and predicate are required"}
            try:
                definition = predicate_definition(clean_predicate)
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            if definition.sensitive and not include_sensitive:
                return {
                    "status": "redacted",
                    "predicate": clean_predicate,
                    "reason": "include_sensitive is required",
                    "facts": [],
                }
            records = await canonical_facts.history(
                memory_space_id,
                clean_subject,
                clean_predicate,
                object_value=clean_object,
                limit=max(1, min(limit, 100)),
            )
            return {
                "status": "ok",
                "memory_space_id": memory_space_id,
                "subject": clean_subject,
                "predicate": clean_predicate,
                "object": clean_object,
                "facts": [record.model_dump(mode="json") for record in records],
            }

    @tool(_AGENT)
    async def eidolon_memory_active_commitments(
        context: dict[str, Any],
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return active commitments visible in the caller's Realm context.

        This is an Agent read contract, not the operator ledger browser below.
        Requiring the same actor context as recall keeps missing identity fail-closed
        and prevents callers from selecting another Palace or audience. A runtime
        without a commitment ledger returns an explicit degraded result.
        """
        ctx = _actor_context_for_surface(context, surface=surface)
        result = await service.read_active_commitments(
            ctx,
            limit=max(1, min(limit, 10)),
        )
        return {
            "memory_space_id": ctx.memory_space_id,
            **result.model_dump(mode="json"),
        }

    if commitments is not None:

        @tool(_OPS)
        async def eidolon_memory_commitments(
            include_terminal: bool = False,
            limit: int = 100,
        ) -> dict[str, Any]:
            """List current commitments, optionally including terminal history."""
            page = await commitments.list_current_page(
                memory_space_id,
                include_terminal=include_terminal,
                limit=max(1, min(limit, 200)),
            )
            return {
                "memory_space_id": memory_space_id,
                "include_terminal": include_terminal,
                "total": page.total,
                "truncated": page.truncated,
                "commitments": [row.model_dump(mode="json") for row in page.commitments],
            }

        @tool(_OPS)
        async def eidolon_memory_commitment_history(
            commitment_id: str,
            limit: int = 200,
        ) -> dict[str, Any]:
            """Read immutable revisions for one Realm-bound commitment."""
            clean_id = (commitment_id or "").strip()
            if not clean_id:
                return {"status": "error", "error": "commitment_id is required"}
            revisions = await commitments.history(
                memory_space_id,
                clean_id,
                limit=max(1, min(limit, 500)),
            )
            return {
                "status": "ok" if revisions else "not_found",
                "memory_space_id": memory_space_id,
                "commitment_id": clean_id,
                "revisions": [row.model_dump(mode="json") for row in revisions],
            }

    if surface == "all" and dlq_store is not None:
        _register_dlq_tools(
            mcp,
            dlq_store=dlq_store,
            replay_publisher=replay_publisher or command_publisher,
        )

    @tool(_OPS)
    async def eidolon_memory_list(
        limit: int = 500,
        offset: int = 0,
        include_private: bool = False,
    ) -> dict[str, Any]:
        """Paginated listing of this memory space's drawers (Admin / IDE)."""
        lim = max(1, min(limit, 5000))
        off = max(0, offset)
        rows = await backend.get_all(memory_space_id, limit=lim, offset=off)
        filtered = [r for r in rows if row_visible_to_listing(r, include_private=include_private)]
        return {
            "records": [wire_record_to_public_dict(r) for r in filtered],
            "total_hint": len(filtered),
        }

    @tool(_OPS)
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

    @tool(_OPS)
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

    @tool(_OPS)
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
        return await build_palace_graph(backend, max_nodes=mn, max_edges=me)

    # Gated as whole groups rather than per tool, because every tool in all three
    # is an operator tool — these are the write and confirm paths, and they are the
    # ones it matters most to keep off the agent's list.
    if surface == "all" and command_publisher is not None:
        _register_privacy_tools(
            mcp,
            backend=backend,
            command_publisher=command_publisher,
            memory_space_id=memory_space_id,
            command_status=command_status,
            commitments=commitments,
            # The service's signer, not a second one. A proof carries a
            # per-instance secret, so a preview minted by ``preview_forget`` and a
            # confirm arriving at this tool have to meet on the same key —
            # otherwise every cross-surface commit fails as forged, and does so
            # only in the deployment where both surfaces are actually used.
            signer=service.privacy_signer,
        )

    if surface == "all" and kg is not None and command_publisher is not None:
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
    signer: PrivacyConfirmationSigner,
    commitments: Any = None,
) -> None:
    """Read-only preview followed by an exact-ID command on the write stream."""

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
                backend,
                memory_space_id,
                clean_target,
                commitments=commitments,
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
            drawer_ids=[
                candidate.key for candidate in candidates if candidate.key.startswith("drawer_")
            ],
            commitment_ids=[
                candidate.key for candidate in candidates if candidate.key.startswith("commitment:")
            ],
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
            commitment_ids=proof.commitment_ids,
            preview_id=proof.preview_id,
            target=proof.target,
        )
        outcome = await publish_with_status(
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
            "commitment_ids": proof.commitment_ids,
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
    projection; read tools query the graph directly. A legacy
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
            outcome = await publish_with_status(
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
            tid = await kg.find_pending_triple_id(f"req:{request_id}", subject, predicate, object)
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
            return await publish_with_status(
                command_publisher,
                command_status,
                cmd,
                wait_seconds=wait_visible_seconds,
            )

        await command_publisher.publish(cmd)
        deadline = time.monotonic() + wait_visible_seconds
        while time.monotonic() < deadline:
            applied = await kg.find_invalidation_applied(subject, predicate, object, ended_iso)
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
        # Every audience: these are operator tools for one space, and an
        # operator inspecting a graph needs to see all of it, not the slice one
        # companion would get. What still gates the health predicates is
        # include_sensitive.
        records = await kg.query_entity(
            name,
            audiences=await _all_audiences(kg),
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
            audiences=await _all_audiences(kg),
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

        ``stats`` plus a capped triple list — wraps
        :meth:`KnowledgeGraphPort.timeline`, which runs under the space lock and
        filters on the ``sensitive`` column. (That column, not predicate names:
        sensitivity is resolved once on write, so the store never hands back a
        health statement the caller did not ask for.)

        ``current_only`` is pushed into the query rather than applied to the
        result. Filtering a ``LIMIT``-ed page in Python returned far fewer than
        ``max_triples`` current triples on any graph with history, and then
        reported ``capped`` against the post-filter count — under-reporting while
        saying it had not.
        """
        limit = max(10, min(max_triples, 5000))
        records = await kg.timeline(
            entity_name=entity if entity else None,
            # Required and keyword-only, and previously omitted — which made every
            # invocation of this tool raise TypeError. Operator tools see every
            # audience; see ``_all_audiences``.
            audiences=await _all_audiences(kg),
            limit=limit,
            current_only=current_only,
            include_sensitive=include_sensitive,
        )
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
