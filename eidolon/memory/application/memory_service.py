"""The service, as one object that serves every space it is asked about.

This is the layer that was missing. Before it, a process *was* a space: handles
were resolved once at startup and captured by every MCP tool, so serving a second
space meant a second process, a second embedding model, and a second port. That
made the resident model a per-space cost and made "one owner, three companions"
cost three processes.

Here a space is a parameter. Every method takes a
:class:`MemoryActorContext`, resolves that caller's handles through the router,
and serves the request. One instance serves any number of spaces; how many a
process actually holds is a deployment decision the router makes, not a shape
baked into the call sites.

Two consequences worth naming, because they are the reason to do this rather than
side effects:

* **The owner layer becomes possible.** ``audience`` distinguishes facts about the
  owner from what happened with one companion, which only means something if both
  companions read the same store. While a space was one companion, they were
  separate stores and the distinction could not do anything.
* **A replica stops being special.** Any replica can serve any request, because
  nothing about which space it serves is fixed at startup.

The contract methods (:class:`MemoryReadContract`) are the public surface. Where
a caller still needs more than the contract exposes, that is served by an
explicitly named internal method rather than a second contract — see
:meth:`recall_fused`.
"""

from __future__ import annotations

import time
from typing import Any

from eidolon_memory_contracts import (
    ActiveCommitment,
    CommitmentReadResult,
    ForgetCandidate,
    ForgetPreview,
    MemoryActorContext,
    MemorySnippet,
    RecallPlan,
    RecallResult,
    SearchResult,
    ServiceStatus,
    SourceTurnLookup,
    WriteOutcome,
)

from eidolon.memory.application.forget import (
    extract_privacy_target,
    find_forget_candidates,
)
from eidolon.memory.application.public_recall import (
    recall_with_kg_fusion,
    wire_record_to_public_dict,
)
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRouter,
    MemorySpaceRuntime,
    MemorySpaceUnavailable,
    UnknownMemorySpace,
)
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class FusedRecall(dict):
    """Recall output including material the read contract deliberately omits.

    ``RecallResult`` carries no ``kg_triples`` and no ``working_memory``: either
    would let a caller infer whether this deployment keeps a graph, which is the
    knowledge the contract exists to withhold.

    This exists because the MCP tool surface returns both today and a client
    reads them. Keeping it as one named type, produced by one method, is what
    stops that from becoming a second contract — when the client stops reading
    those fields, this type and its method go, and nothing else changes.
    """


class MemoryService:
    """Reads and writes for any space, resolved per request.

    Never raises for a storage failure. A recall that cannot reach its store
    returns ``degraded=True`` with a reason, because the caller is assembling a
    reply and an exception there costs the whole turn rather than the memories.
    Programming errors still raise — a caller passing an invalid context should
    hear about it.
    """

    def __init__(
        self,
        router: MemorySpaceRouter,
        settings: MemorySettings,
        *,
        command_publisher: Any = None,
    ) -> None:
        self._router = router
        self._settings = settings
        self._command_publisher = command_publisher

    # ── resolution ───────────────────────────────────────────────────────────

    async def _runtime(self, ctx: MemoryActorContext) -> MemorySpaceRuntime:
        """This caller's handles.

        Raises rather than degrading: being asked about a space this deployment
        does not serve is a routing fault, and answering it with an empty recall
        would look to a user like their companion had forgotten them.
        """

        return await self._router.resolve(ctx.memory_realm_id)

    # ── the read contract ────────────────────────────────────────────────────

    async def recall_context(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        plan: RecallPlan | None = None,
        timeout_s: float = 0.2,
    ) -> RecallResult:
        """Recall what matters for this caller's turn.

        The contract's shape: a rendered block plus itemised snippets, and no
        indication of which internal signals produced them.
        """

        fused = await self.recall_fused(ctx, query, plan=plan, timeout_s=timeout_s)
        return RecallResult(
            context=fused["context"],
            snippets=fused["snippets"],
            degraded=fused["degraded"],
            degraded_reason=fused["degraded_reason"],
        )

    async def search(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        top_k: int = 5,
        timeout_s: float = 0.2,
    ) -> SearchResult:
        """Look up memories matching ``query``, without conversational ranking."""

        try:
            runtime = await self._runtime(ctx)
            records = await runtime.backend.search(query, n_results=max(1, top_k))
        except (UnknownMemorySpace, MemorySpaceUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            log.warning("search_degraded", memory_space_id=ctx.memory_realm_id, error=str(exc))
            return SearchResult(degraded=True, degraded_reason=str(exc))

        return SearchResult(snippets=[_snippet(record) for record in records])

    async def health(self) -> bool:
        """Whether this instance can serve at all.

        Deliberately not per-space: a caller asking this wants to know if the
        service is up, and resolving a space to answer it would make the check
        cost a store open.
        """

        return True

    # ── beyond the contract, on purpose ──────────────────────────────────────

    async def recall_fused(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        plan: RecallPlan | None = None,
        timeout_s: float = 0.2,
        include_sensitive_kg: bool = False,
    ) -> FusedRecall:
        """Recall including the graph triples and recent turns, for the MCP surface.

        Named separately from :meth:`recall_context` so the extra material has one
        producer and one consumer. See :class:`FusedRecall` for why it exists and
        when it goes.
        """

        plan = plan or RecallPlan()
        started = time.perf_counter()
        kind = "voice" if plan.voice else "chat"

        try:
            runtime = await self._runtime(ctx)
        except (UnknownMemorySpace, MemorySpaceUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("recall_resolve_failed", memory_space_id=ctx.memory_realm_id, error=str(exc))
            return _degraded_recall(str(exc))

        want_kg = self._settings.recall.kg_in_recall and runtime.has_kg
        subjects = [
            value.strip()
            for value in plan.focus_subjects[: self._settings.recall.kg_max_entities]
            if value and value.strip()
        ]

        try:
            fused = await recall_with_kg_fusion(
                runtime.backend,
                self._settings,
                query=query,
                context=ctx,
                top_k=max(1, plan.semantic_k),
                kg=runtime.kg if want_kg else None,
                for_voice=plan.voice,
                palace_path=runtime.palace_path,
                include_sensitive_kg=include_sensitive_kg,
                kg_subjects=subjects,
            )
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            log.warning("recall_degraded", memory_space_id=ctx.memory_realm_id, error=str(exc))
            metrics.RECALL_TOTAL.labels(kind=kind, outcome="degraded").inc()
            return _degraded_recall(str(exc))

        records = fused["vector"]
        kg_records = fused["kg"]
        turns = fused.get("working_memory") or []

        metrics.RECALL_SECONDS.labels(
            kind=kind,
            backend="configured",
            graph="on" if want_kg else "off",
            degraded="false",
        ).observe(time.perf_counter() - started)
        metrics.RECALL_TOTAL.labels(
            kind=kind, outcome="hit" if records or kg_records else "empty"
        ).inc()

        result = FusedRecall()
        result["context"] = group_recall_context(
            records, kg_triples=kg_records, working_memory=turns
        )
        result["snippets"] = [_snippet(record) for record in records]
        result["records"] = [wire_record_to_public_dict(record) for record in records]
        result["kg_triples"] = [triple.model_dump(mode="json") for triple in kg_records]
        result["working_memory"] = [turn.model_dump(mode="json") for turn in turns]
        # Read from the fusion rather than assumed: it catches a failed wing
        # search internally and returns an empty list, so "no results" and "we
        # could not look properly" are indistinguishable without this flag. To an
        # operator those mean opposite things.
        degraded = bool(fused.get("degraded"))
        result["degraded"] = degraded
        result["degraded_reason"] = fused.get("degraded_reason") if degraded else None
        return result

    # ── the rest of the read contract ────────────────────────────────────────

    async def read_active_commitments(
        self,
        ctx: MemoryActorContext,
        *,
        limit: int = 5,
        timeout_s: float = 0.2,
    ) -> CommitmentReadResult:
        """Promises still in play for this caller.

        A deployment without the commitment ledger answers ``degraded`` rather
        than empty: no promises and "we cannot see the promises" are different
        answers, and only one of them should make a companion say there are none.
        """

        runtime = await self._runtime(ctx)
        ledger = runtime.ledgers.commitments
        if ledger is None:
            return CommitmentReadResult(
                degraded=True, degraded_reason="commitment ledger not configured"
            )

        try:
            page = await ledger.list_current_page(
                ctx.memory_realm_id, include_terminal=False, limit=max(1, min(limit, 200))
            )
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            log.warning("commitments_degraded", memory_space_id=ctx.memory_realm_id, error=str(exc))
            return CommitmentReadResult(degraded=True, degraded_reason=str(exc))

        return CommitmentReadResult(
            commitments=[_active_commitment(row) for row in page.items]
        )

    async def get_by_source_turn(
        self,
        ctx: MemoryActorContext,
        source_turn_id: str,
        *,
        timeout_s: float = 0.5,
    ) -> SourceTurnLookup:
        """What a published turn produced, if it has been absorbed yet.

        ``absorbed=False`` with no error is the normal answer for a turn still in
        flight — writes are asynchronous, so "not yet" is not a failure.
        """

        runtime = await self._runtime(ctx)
        try:
            records = await runtime.backend.get_by_source_turn_id(
                ctx.memory_realm_id, source_turn_id
            )
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            log.warning(
                "source_turn_lookup_degraded",
                memory_space_id=ctx.memory_realm_id,
                error=str(exc),
            )
            return SourceTurnLookup(
                source_turn_id=source_turn_id, degraded=True, degraded_reason=str(exc)
            )

        return SourceTurnLookup(
            source_turn_id=source_turn_id,
            found=bool(records),
            snippets=[_snippet(record) for record in records],
        )

    async def preview_forget(
        self,
        ctx: MemoryActorContext,
        query: str,
        *,
        action: str = "archive",
        timeout_s: float = 0.5,
    ) -> ForgetPreview:
        """Resolve a privacy request without changing anything.

        On the read side because it has no side effects; committing is a write.
        A preview that silently covered only part of what matched would be worse
        than an error, so the underlying scan raises on hitting its bound rather
        than truncating — and that surfaces here as degraded.
        """

        runtime = await self._runtime(ctx)
        target = extract_privacy_target(query)
        try:
            candidates = await find_forget_candidates(
                runtime.backend, ctx.memory_realm_id, target
            )
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            log.warning("forget_preview_degraded", memory_space_id=ctx.memory_realm_id, error=str(exc))
            return ForgetPreview(
                status="failed", target=target, action=action, error=str(exc)
            )

        if not candidates:
            # Distinct from a failure: the request was understood and matched
            # nothing, which the caller should tell the user rather than retry.
            return ForgetPreview(status="not_found", target=target, action=action)

        return ForgetPreview(
            status="preview",
            target=target,
            action=action,
            candidates=[
                ForgetCandidate(
                    id=str(getattr(c, "key", "") or ""),
                    text=str(getattr(c, "text", "") or getattr(c, "value", "") or ""),
                    score=float(getattr(c, "score", 0.0) or 0.0),
                )
                for c in candidates
            ],
            # Forgetting is irreversible, so the caller must show what will go and
            # hand the token back. Minting it is a write concern, not this method's.
            requires_explicit_confirmation=True,
        )

    async def command_status(
        self,
        ctx: MemoryActorContext,
        request_id: str,
        *,
        timeout_s: float = 0.5,
    ) -> WriteOutcome:
        """The current outcome of an earlier non-terminal write.

        Without the ledger this reports ``unknown`` rather than inventing a
        status: a caller that heard "applied" for a command whose fate is
        unrecorded would be misled in the one direction that matters.
        """

        runtime = await self._runtime(ctx)
        ledger = runtime.ledgers.command_status
        if ledger is None:
            return WriteOutcome(
                status="unknown",
                request_id=request_id,
                error="command status ledger not configured",
            )

        try:
            record = await ledger.wait_terminal(request_id, timeout_seconds=timeout_s)
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            return WriteOutcome(status="unknown", request_id=request_id, error=str(exc))

        if record is None:
            return WriteOutcome(status="unknown", request_id=request_id)
        return WriteOutcome(
            status=record.status,
            request_id=request_id,
            resource_id=record.resource_id,
            error=record.error,
        )

    async def status(self, ctx: MemoryActorContext) -> ServiceStatus:
        """Operational summary for this caller's space.

        Diagnostics, not control flow: a caller must not branch on these fields,
        because which of them are populated depends on the deployment.
        """

        runtime = await self._runtime(ctx)
        return ServiceStatus(
            memory_space_id=ctx.memory_realm_id,
            ready=True,
            # Whether a graph is configured belongs in details, not in a top-level
            # field: the contract's point is that a caller cannot discover it and
            # branch on it. Operators read this; clients must not.
            details={
                "graph_configured": runtime.has_kg,
                "spaces_held": len(await self.held_spaces()),
            },
        )

    async def held_spaces(self) -> list[str]:
        """Which spaces this instance currently holds handles for.

        Operational, not part of the contract: it reports on this process rather
        than on a caller's memories.
        """

        holder = getattr(self._router, "held_spaces", None)
        return list(holder()) if holder is not None else []


def _degraded_recall(reason: str) -> FusedRecall:
    result = FusedRecall()
    result["context"] = ""
    result["snippets"] = []
    result["records"] = []
    result["kg_triples"] = []
    result["working_memory"] = []
    result["degraded"] = True
    result["degraded_reason"] = reason
    return result


def _active_commitment(row: Any) -> ActiveCommitment:
    """One commitment row as the contract shape."""

    return ActiveCommitment(
        commitment_id=str(getattr(row, "commitment_id", "")),
        promisor=str(getattr(row, "promisor", "")),
        predicate=getattr(row, "predicate", "promised"),
        action=str(getattr(row, "action_value", "") or getattr(row, "action", "")),
        status=getattr(row, "status", "proposed"),
        beneficiaries=tuple(getattr(row, "beneficiaries", ()) or ()),
        participants=tuple(getattr(row, "participants", ()) or ()),
        condition=getattr(row, "condition_value", None),
        due_at=getattr(row, "due_at", None),
        revision=int(getattr(row, "revision", 1) or 1),
        updated_at=str(getattr(row, "updated_at", "") or ""),
    )


def _snippet(record: Any) -> MemorySnippet:
    """One store record as the contract's snippet shape.

    Reads only the hot-path fields RECALL_HOT_PATH_FIELDS pins, so a different
    vector store remains a plausible substitution.
    """

    return MemorySnippet(
        id=str(getattr(record, "key", "") or getattr(record, "source_file", "") or ""),
        text=str(getattr(record, "value", "") or ""),
        similarity=float(getattr(record, "similarity", 0.0) or 0.0),
        metadata={
            "wing": getattr(record, "wing", None),
            "room": getattr(record, "room", None),
        },
    )
