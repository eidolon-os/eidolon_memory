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
import uuid
from datetime import UTC, datetime
from typing import Any

from eidolon_memory_contracts import (
    ActiveCommitment,
    CommitmentReadResult,
    ConversationTurnPayload,
    ForgetCandidate,
    ForgetOutcome,
    ForgetPreview,
    MemoryActorContext,
    MemorySnippet,
    PrivacyMutationCommand,
    RecallPlan,
    RecallResult,
    SearchResult,
    ServiceStatus,
    SourceTurnLookup,
    TurnPublishReceipt,
    WriteOutcome,
    conversation_turn_subject,
)

from eidolon.memory.application.command_delivery import publish_with_status
from eidolon.memory.application.forget import (
    find_forget_candidates,
    normalize_privacy_target,
)
from eidolon.memory.application.materialization import inspect_materialization
from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner
from eidolon.memory.application.public_recall import (
    recall_with_kg_fusion,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import CommandStatusStore
from eidolon.memory.domain.space_runtime import (
    MemorySpaceRouter,
    MemorySpaceRuntime,
    MemorySpaceUnavailable,
    UnknownMemorySpace,
)
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minted(
    signer: PrivacyConfirmationSigner,
    *,
    memory_space_id: str,
    action: str,
    target: str,
    drawer_ids: list[str],
    commitment_ids: list[str],
) -> dict[str, str]:
    """The token fields for a preview, or none if a token cannot cover it.

    ``issue`` refuses more than a hundred ids, or any that is not a drawer. Both
    are real answers rather than errors here: the candidates are still worth
    showing, and a preview that cannot be committed as one batch should say so by
    carrying no token rather than by failing the whole preview.
    """

    try:
        token, proof = signer.issue(
            memory_space_id=memory_space_id,
            action=action,  # type: ignore[arg-type]
            target=target,
            drawer_ids=drawer_ids,
            commitment_ids=commitment_ids,
        )
    except ValueError as exc:
        log.info("forget_preview_not_tokenisable", target=target, reason=str(exc))
        return {}
    # ISO-8601, because every other timestamp a caller sees is. The proof keeps
    # a unix integer internally, which is right for comparing against a clock and
    # wrong for handing to someone who has to read or log it.
    expires = datetime.fromtimestamp(proof.expires_at, tz=UTC)
    return {
        "confirmation_token": token,
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class FusedRecall(dict):
    """Prompt-ready recall plus projection evidence for the Agent boundary."""


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
        command_status: CommandStatusStore | None = None,
        turn_publisher: Any = None,
        signer: PrivacyConfirmationSigner | None = None,
    ) -> None:
        self._router = router
        self._settings = settings
        self._command_publisher = command_publisher
        self._command_status = command_status
        self._turn_publisher = turn_publisher
        # One signer, because a proof carries a per-instance secret: a token
        # minted by ``preview_forget`` is verifiable only by the same object.
        # Whoever else needs to verify one — the MCP surface does — must be handed
        # this instance rather than construct its own, or a preview and its
        # confirmation land on different keys and every commit fails as forged.
        self._signer = signer or PrivacyConfirmationSigner()

    @property
    def privacy_signer(self) -> PrivacyConfirmationSigner:
        """The signer this service mints and verifies forget tokens with.

        Exposed so a second surface can share it. See ``__init__``.
        """

        return self._signer

    # ── resolution ───────────────────────────────────────────────────────────

    async def runtime_for(self, ctx: MemoryActorContext) -> MemorySpaceRuntime:
        """This caller's handles.

        Raises rather than degrading: being asked about a space this deployment
        does not serve is a routing fault, and answering it with an empty recall
        would look to a user like their companion had forgotten them.

        Public because a few operator paths need a raw handle for something the
        contract does not cover — scoping a search to one wing, for instance.
        Reaching for it is a sign the contract is missing something; it is not
        the normal way to use this class.
        """

        return await self._router.resolve(ctx.memory_realm_id)

    async def runtime_for_space(self, memory_space_id: str) -> MemorySpaceRuntime:
        """Handles for a named space, with no actor claimed.

        The operator surface inspects a space rather than acting as someone
        inside it. Routing that through :meth:`runtime_for` would mean minting a
        ``MemoryActorContext`` with no owner and no companion, and an empty
        actor context is exactly the shape the audience filter reads as a
        caller. Saying plainly that there is no actor is safer than inventing
        one that looks like an anonymous participant.
        """

        return await self._router.resolve(memory_space_id)

    async def _runtime(self, ctx: MemoryActorContext) -> MemorySpaceRuntime:
        return await self.runtime_for(ctx)

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
        """Look up memories matching ``query``, without conversational ranking.

        A lookup, not a recall: the graph, recent turns and session filtering are
        deliberately absent. A user asking "what do you remember about X" wants
        what is stored, not what would be relevant to the current turn.

        Still searches every wing — scoping to one is a separate, operator-facing
        question and not part of this contract.
        """

        try:
            runtime = await self._runtime(ctx)
            records = await search_all_wings_mcp_style(
                runtime.backend,
                self._settings,
                query=query,
                context=ctx,
                top_k=max(1, top_k),
                wing=None,
                room=None,
                for_voice=False,
                palace_path=runtime.palace_path,
                # Otherwise a failed wing search is swallowed and returns an empty
                # list, so "nothing stored" and "could not look" arrive identically.
                # Raises only when the store degraded *and* nothing was found:
                # partial results are still an answer, and the caller gets them.
                raise_on_degraded=True,
            )
        except (UnknownMemorySpace, MemorySpaceUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            # Note the cost of this invariant: a programming error in here comes
            # back as a degraded result rather than a traceback. A caller sees
            # "no memories" and carries on. That is right for the caller and
            # dangerous for us, so the reason is always logged with the space id.
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
        include_kg: bool | None = None,
    ) -> FusedRecall:
        """Recall including the graph triples and recent turns, for the MCP surface.

        Named separately from :meth:`recall_context` so the extra material has one
        producer and one consumer. See :class:`FusedRecall` for why it exists and
        when it goes.
        """

        plan = plan or RecallPlan()
        kind = "voice" if plan.voice else "chat"
        service_started = time.perf_counter()
        resolution_started = time.perf_counter()

        try:
            runtime = await self._runtime(ctx)
        except (UnknownMemorySpace, MemorySpaceUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "recall_resolve_failed",
                memory_space_id=ctx.memory_realm_id,
                error=str(exc),
            )
            return _degraded_recall(
                str(exc),
                trace={
                    "runtime_resolution_ms": _elapsed_ms(resolution_started),
                    "service_total_ms": _elapsed_ms(service_started),
                },
            )
        runtime_resolution_ms = _elapsed_ms(resolution_started)

        # A caller may turn the graph off for one request; it can never turn one
        # on that this deployment does not have.
        wanted = self._settings.recall.kg_in_recall if include_kg is None else include_kg
        want_kg = bool(wanted) and runtime.has_kg
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
            return _degraded_recall(
                str(exc),
                trace={
                    "runtime_resolution_ms": runtime_resolution_ms,
                    "service_total_ms": _elapsed_ms(service_started),
                },
            )

        records = fused["vector"]
        kg_records = fused["kg"]

        # Not recorded here. ``recall_with_kg_fusion`` already called
        # ``_record_recall`` on its way out, with the labels this layer cannot
        # supply: the real backend name rather than the literal ``"configured"``,
        # and the degradation flag rather than a hardcoded ``"false"``.
        #
        # Both metrics were being written twice per recall. ``RECALL_TOTAL`` simply
        # counted double, so every rate built on it was 2x. ``RECALL_SECONDS`` was
        # worse: the duplicate carried a different ``backend`` label, so one recall
        # produced two histogram series and neither was the whole picture.
        #
        # The degraded path above keeps its own increment — an exception means the
        # inner recorder never ran, and that outcome would otherwise go unrecorded.

        result = FusedRecall()
        result["context"] = group_recall_context(records, kg_triples=kg_records)
        result["snippets"] = [_snippet(record) for record in records]
        result["records"] = [wire_record_to_public_dict(record) for record in records]
        result["kg_triples"] = [triple.model_dump(mode="json") for triple in kg_records]
        # Read from the fusion rather than assumed: it catches a failed wing
        # search internally and returns an empty list, so "no results" and "we
        # could not look properly" are indistinguishable without this flag. To an
        # operator those mean opposite things.
        degraded = bool(fused.get("degraded"))
        result["degraded"] = degraded
        result["degraded_reason"] = fused.get("degraded_reason") if degraded else None
        result["trace"] = {
            **dict(fused.get("trace") or {}),
            "runtime_resolution_ms": runtime_resolution_ms,
            "service_total_ms": _elapsed_ms(service_started),
        }
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
            commitments=[_active_commitment(row) for row in page.commitments],
            total=page.total,
            truncated=page.truncated,
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
        target = normalize_privacy_target(query)
        try:
            candidates = await find_forget_candidates(
                runtime.backend,
                ctx.memory_realm_id,
                target,
                commitments=runtime.ledgers.commitments,
            )
        except Exception as exc:  # noqa: BLE001 - contract: never raise on storage
            log.warning(
                "forget_preview_degraded",
                memory_space_id=ctx.memory_realm_id,
                error=str(exc),
            )
            return ForgetPreview(status="failed", target=target, action=action, error=str(exc))

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
            requires_explicit_confirmation=True,
            # Minted here, and this used to be left empty on the reasoning that
            # "minting is a write concern". It is not: a token describes what
            # *would* be written and changes nothing. Leaving it blank while
            # setting ``requires_explicit_confirmation`` meant a caller following
            # the contract literally could never commit — it was told to hand back
            # a token it had not been given. Nobody hit it because the MCP surface
            # mints its own; the read contract's own path was the broken one.
            **_minted(
                self._signer,
                memory_space_id=ctx.memory_realm_id,
                action=action,
                target=target,
                drawer_ids=[
                    str(getattr(c, "key", "") or "")
                    for c in candidates
                    if str(getattr(c, "key", "") or "").startswith("drawer_")
                ],
                commitment_ids=[
                    str(getattr(c, "key", "") or "")
                    for c in candidates
                    if str(getattr(c, "key", "") or "").startswith("commitment:")
                ],
            ),
        )

    # ── the write contract ───────────────────────────────────────────────────
    #
    # Declared since the contracts package existed and implemented by nothing
    # until 2026-08-07. The three operations were all real — a turn goes out over
    # NATS, explicit writes and forgets go through MCP tools — but they were
    # scattered across two transports with no object gathering them, so the rule
    # the contract is built around had no single place to hold:
    #
    #     ``applied`` is the only status that means stored and readable. A caller
    #     that says "I'll remember that" on ``accepted`` is lying to the user.
    #
    # It holds here now, for every explicit write, whichever surface asked.

    async def publish_turn(
        self,
        turn: ConversationTurnPayload,
        *,
        trace_id: str | None = None,
    ) -> TurnPublishReceipt:
        """Hand a completed turn to memory. Fire and forget, and says so.

        The receipt reports whether the *bus* took the message — never whether
        anything was remembered, which is not knowable yet and is the steward's
        decision minutes later. Deduplicated by ``turn.turn_id``.
        """

        memory_space_id = (turn.context.memory_space_id or "").strip()
        if self._turn_publisher is None:
            # A deployment with no bus wired is a real configuration, not an
            # error: in-process callers exist. ``skipped_no_bus`` exists in the
            # receipt for exactly this, and is not ``published``.
            return TurnPublishReceipt(
                turn_id=turn.turn_id,
                memory_space_id=memory_space_id,
                state="skipped_no_bus",
                trace_id=trace_id,
            )
        try:
            await self._turn_publisher.publish_turn(turn)
        except Exception as exc:  # noqa: BLE001 - the caller gets a receipt, not a raise
            log.warning(
                "turn_publish_failed",
                turn_id=turn.turn_id,
                memory_space_id=memory_space_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return TurnPublishReceipt(
                turn_id=turn.turn_id,
                memory_space_id=memory_space_id,
                state="publish_failed",
                error=str(exc),
                trace_id=trace_id,
            )
        return TurnPublishReceipt(
            turn_id=turn.turn_id,
            memory_space_id=memory_space_id,
            state="published",
            subject=conversation_turn_subject(memory_space_id) if memory_space_id else None,
            trace_id=trace_id,
        )

    async def confirm_forget(
        self,
        ctx: MemoryActorContext,
        confirmation_token: str,
        *,
        wait_applied_seconds: float = 2.0,
    ) -> ForgetOutcome:
        """Commit a privacy request previewed earlier.

        The token is scoped to the candidates the user was shown. A token that is
        expired, forged, or for another space fails — **it never widens into a
        broader deletion**, which is the one failure mode that would be worse
        than refusing.
        """

        try:
            proof = self._signer.verify(
                (confirmation_token or "").strip(),
                expected_memory_space_id=ctx.memory_realm_id,
            )
        except ValueError as exc:
            return ForgetOutcome(status="failed", action="archive", error=str(exc))

        if self._command_publisher is None:
            return ForgetOutcome(
                status="unavailable",
                action=proof.action,
                error="no command publisher configured",
            )

        command = PrivacyMutationCommand(
            request_id=uuid.uuid4().hex,
            memory_space_id=ctx.memory_realm_id,
            issued_at=_now_iso(),
            issuer="agent",
            action=proof.action,
            drawer_ids=proof.drawer_ids,
            commitment_ids=proof.commitment_ids,
            preview_id=proof.preview_id,
            target=proof.target,
        )
        outcome = await publish_with_status(
            self._command_publisher,
            self._command_status,
            command,
            wait_seconds=wait_applied_seconds,
        )
        status = outcome.get("status", "accepted")
        return ForgetOutcome(
            # ``retrying`` and ``unknown`` are honest here as "not yet", which is
            # what ``accepted`` means in this narrower vocabulary. Only the
            # ledger saying ``applied`` earns ``applied``.
            status=status if status in {"accepted", "applied", "failed"} else "accepted",
            action=proof.action,
            request_id=str(outcome.get("request_id") or command.request_id),
            forgotten_ids=(
                [*proof.drawer_ids, *proof.commitment_ids] if status == "applied" else []
            ),
            error=str(outcome.get("error") or ""),
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
        status = await inspect_materialization(runtime)
        status.details.update(
            {
                # Operator-only diagnostics, not client capability flags.
                "graph_configured": runtime.has_kg,
                "spaces_held": len(await self.held_spaces()),
            }
        )
        return status

    async def held_spaces(self) -> list[str]:
        """Which spaces this instance currently holds handles for.

        Operational, not part of the contract: it reports on this process rather
        than on a caller's memories.
        """

        holder = getattr(self._router, "held_spaces", None)
        return list(holder()) if holder is not None else []


def _degraded_recall(reason: str, *, trace: dict[str, float] | None = None) -> FusedRecall:
    result = FusedRecall()
    result["context"] = ""
    result["snippets"] = []
    result["records"] = []
    result["kg_triples"] = []
    result["degraded"] = True
    result["degraded_reason"] = reason
    result["trace"] = dict(trace or {})
    return result


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


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
