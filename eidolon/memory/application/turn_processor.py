"""Process JetStream messages: ConversationTurn + MemoryCommand (KG plan §4.4).

Used by ``agent_runner``'s in-process NATS subscriber. Steward runs in-process
returning a ``StewardDecision``; this module registers every accepted fact in
the canonical ledger and then applies its drawer/KG projections. A projection
failure is retryable and is never acknowledged as if every projection agreed.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Protocol

from eidolon_memory_contracts import (
    OWNER_AUDIENCE,
    ConsolidatorIngestThemeCommand,
    ConversationTurnPayload,
    DeviceSyncBatchPayload,
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryCommandPayload,
    MemoryIntent,
    MemoryIntentCommand,
    PrivacyMutationCommand,
    envelope_memory_payload,
    parse_conversation_turn,
    parse_memory_command,
)
from pydantic import ValidationError

from eidolon.memory.application.canonical_invalidation import (
    invalidate_exact_canonical_fact,
)
from eidolon.memory.application.explicit_intents import (
    MemoryIntentRejected,
    _projection_room_token,
    apply_explicit_intent,
)
from eidolon.memory.application.forget import (
    forget_resolved_projections,
)
from eidolon.memory.application.ingest import (
    ingest_memory_fragment,
    ingest_memory_fragments,
)
from eidolon.memory.application.memory_intents import memory_intents_from_decision
from eidolon.memory.application.scope_policy import (
    MissingInteractionIdentity,
    interaction_audience,
    interaction_readable_audiences,
)
from eidolon.memory.application.steward.common import (
    apply_privacy_actions,
    stamp_fragment_identity,
)
from eidolon.memory.config.memory_settings import MemorySettings, resolve_dlq_log_path
from eidolon.memory.domain.command_status import CommandStatus
from eidolon.memory.domain.extraction_decision import (
    ExtractionDecisionConflict,
    ExtractionDecisionRecord,
    extraction_input_hash,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import (
    CanonicalFactWriter,
    CommandStatusWriter,
    CommitmentWriter,
    DlqWriter,
    ExtractionDecisionStore,
)
from eidolon.memory.domain.predicates import fact_sentence
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class StewardProtocol(Protocol):
    extraction_version: str

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision: ...


async def _decide_once(
    steward: StewardProtocol,
    turn: ConversationTurnPayload,
    store: ExtractionDecisionStore | None,
) -> tuple[StewardDecision, list[MemoryIntent]]:
    """Return one durable extraction result for a turn + extractor version."""
    if store is None:
        decision = await steward.decide(turn)
        return decision, memory_intents_from_decision(
            decision,
            memory_space_id=turn.context.memory_space_id,
            source_event_id=turn.turn_id,
        )

    extractor_version = str(getattr(steward, "extraction_version", "")).strip()
    if not extractor_version:
        raise ValueError("steward must expose a non-empty extraction_version")
    input_hash = extraction_input_hash(turn)
    existing = await store.get(
        turn.context.memory_space_id,
        turn.turn_id,
        extractor_version,
    )
    if existing is not None:
        if existing.redacted:
            log.info(
                "turn_processor_decision_redacted",
                memory_space_id=turn.context.memory_space_id,
                turn_id=turn.turn_id,
            )
            return existing.decision, []
        if existing.input_hash != input_hash:
            raise ExtractionDecisionConflict(
                "stored extraction decision input does not match redelivered turn"
            )
        log.info(
            "turn_processor_decision_reused",
            memory_space_id=turn.context.memory_space_id,
            turn_id=turn.turn_id,
            extractor_version=extractor_version,
        )
        intents = existing.intents or memory_intents_from_decision(
            existing.decision,
            memory_space_id=turn.context.memory_space_id,
            source_event_id=turn.turn_id,
        )
        return existing.decision, intents

    decision = await steward.decide(turn)
    intents = memory_intents_from_decision(
        decision,
        memory_space_id=turn.context.memory_space_id,
        source_event_id=turn.turn_id,
    )
    stored = await store.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=turn.context.memory_space_id,
            source_turn_id=turn.turn_id,
            extractor_version=extractor_version,
            input_hash=input_hash,
            decision=decision,
            intents=intents,
        )
    )
    if stored.redacted:
        return stored.decision, []
    log.info(
        "turn_processor_decision_persisted",
        memory_space_id=turn.context.memory_space_id,
        turn_id=turn.turn_id,
        extractor_version=extractor_version,
    )
    intents = stored.intents or memory_intents_from_decision(
        stored.decision,
        memory_space_id=turn.context.memory_space_id,
        source_event_id=turn.turn_id,
    )
    return stored.decision, intents


def append_dlq(settings: MemorySettings, payload: bytes, error: str, deliveries: int) -> None:
    """Append a poison message + error to ``settings.nats.dlq_log_path`` (one JSON per line)."""
    path = resolve_dlq_log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "error": error,
        "deliveries": deliveries,
        "payload_preview": payload[:500].decode("utf-8", errors="replace"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def _record_dlq(
    writer: DlqWriter | None,
    settings: MemorySettings,
    msg: Any,
    error: str,
    deliveries: int,
) -> None:
    """Persist recoverable bytes in production; retain JSONL as compatibility fallback."""
    if writer is None:
        append_dlq(settings, msg.data, error, deliveries)
        return
    await writer.add(
        subject=str(getattr(msg, "subject", "") or ""),
        payload=bytes(msg.data),
        error=error,
        deliveries=deliveries,
    )


def delivery_count(msg: Any) -> int:
    meta = getattr(msg, "metadata", None)
    if meta is None:
        return 1
    return int(getattr(meta, "num_delivered", None) or 1)


def _stamped_for_turn(
    fragment: MemoryFragment,
    *,
    turn: ConversationTurnPayload,
    turn_ts: str,
) -> MemoryFragment:
    """Give a fragment its identity and, failing its own, the turn's timestamp."""

    stamped = stamp_fragment_identity(
        fragment,
        context=turn.context,
        source_turn_id=turn.turn_id,
    )
    if stamped.occurred_at:
        return stamped
    return stamped.model_copy(update={"occurred_at": turn_ts})


def _canonical_drawer(
    fragment: MemoryFragment,
    *,
    assertion_id: str,
    evidence_id: str,
    projection_id: str,
) -> MemoryFragment:
    """Bind one Chroma projection to its ledger assertion/outbox identity."""

    return fragment.model_copy(
        update={
            "memory_id": f"canonical:{projection_id}",
            "source_turn_id": f"canonical:{projection_id}",
            "metadata": {
                **fragment.metadata,
                "assertion_id": assertion_id,
                "evidence_id": evidence_id,
                "projection_id": projection_id,
                "source_event_id": fragment.source_turn_id,
                "source": "canonical-natural",
            },
        }
    )


def _drawer_for_triple(
    triple: Any,
    *,
    turn: ConversationTurnPayload,
    turn_ts: str,
    assertion_id: str,
    evidence_id: str,
    projection_id: str,
) -> MemoryFragment:
    """Create the text/vector projection of one structured natural assertion."""

    base = MemoryFragment(
        memory_space_id=turn.context.memory_space_id,
        source_turn_id=turn.turn_id,
        wing="Wing_Profile",
        room=f"fact_{triple.predicate}_{_projection_room_token(projection_id)}",
        # The same sentence the read path renders, from the same table. This
        # used to be a bare f-string, so the text that got embedded read
        # "self owns pet:铁锤" while the query it had to match read
        # "我家狗多大" — 18 of 38 drawers in a benchmark palace were
        # unreachable by the Chinese embedder that way.
        content=fact_sentence(triple.subject, triple.predicate, triple.object),
        memory_type=(
            "preference" if triple.predicate in {"likes", "dislikes", "prefers"} else "fact"
        ),
        importance=4,
        confidence=triple.confidence,
        occurred_at=triple.valid_from or turn_ts,
    )
    stamped = _stamped_for_turn(base, turn=turn, turn_ts=turn_ts)
    return _canonical_drawer(
        stamped,
        assertion_id=assertion_id,
        evidence_id=evidence_id,
        projection_id=projection_id,
    )


class _TurnStages:
    """Per-turn stage timings: histogram samples plus one greppable log line.

    The histogram answers "is absorption getting slower"; it cannot answer
    "which stage owned *that* turn", because a percentile has no turn id. An
    end-to-end latency outlier only ever raises the second question, so the
    same measurements are also emitted on the existing ``turn_processed``
    line, where they sit beside the turn id already logged there.

    A stage that did no work is never entered, so a quiet turn does not push
    zeros into the histogram and flatten its tail.
    """

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._elapsed: dict[str, float] = {}

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time one stage whether it returns or raises.

        Both paths are recorded for the reason the steward stage already did
        it by hand: a stage that reports only on success hides exactly the
        runs an operator went looking for.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self._elapsed[name] = self._elapsed.get(name, 0.0) + elapsed
            metrics.TURN_STAGE_SECONDS.labels(stage=name).observe(elapsed)

    def observe_total(self) -> None:
        """Close the whole-turn span. Only absorption reaches this.

        A turn rejected before the steward ran spent no time in any stage, so
        recording it as a ``total`` sample would report the discard path as
        fast absorption.
        """
        elapsed = time.perf_counter() - self._started
        self._elapsed["total"] = elapsed
        metrics.TURN_STAGE_SECONDS.labels(stage="total").observe(elapsed)

    def log_fields(self) -> dict[str, int]:
        return {f"{name}_ms": round(value * 1000) for name, value in self._elapsed.items()}


async def _apply_privacy(
    backend: Any,
    memory_space_id: str,
    actions: list,
    audiences: tuple[str, ...],
    kg: Any = None,
    canonical_facts: CanonicalFactWriter | None = None,
    commitments: CommitmentWriter | None = None,
    decision_store: ExtractionDecisionStore | None = None,
) -> Any:
    """Wrapper for the steward's privacy-action handler (delete / archive).

    ``kg`` is threaded through because this is how a forget usually arrives —
    the person says "忘掉…" and the steward acts on it, with no tool call and no
    confirmation round trip. Without it the drawer went and the triple stayed.
    """

    if not actions:
        return None
    return await apply_privacy_actions(
        backend,
        memory_space_id=memory_space_id,
        actions=actions,
        audiences=audiences,
        kg=kg,
        canonical_facts=canonical_facts,
        commitments=commitments,
        decision_store=decision_store,
    )


async def process_turn_message(
    msg: Any,
    *,
    steward: StewardProtocol,
    backend: Any,
    kg: Any = None,
    settings: MemorySettings,
    max_deliveries: int,
    expected_memory_space_id: str | None = None,
    #: Optional observer of turn absorption. Duck-typed on ``record_absorbed`` and
    #: ``record_rejected``; see the calls below for the arguments.
    #:
    #: **Nothing implements it today, and that is deliberate rather than an
    #: oversight.** The one implementation wrote into eidolon_data's event log so
    #: an audit view could show the agent→memory handshake closing end to end
    #: rather than only "we tried"; it was deleted on 2026-08-07 along with the
    #: rest of that integration, which targeted an interface that repository has
    #: replaced. It was never active either way — ``agent_runner`` has always
    #: passed ``None`` here.
    #:
    #: The hook stays because the question it answers is a real one and does not
    #: depend on who answers it. A host that wants the handshake auditable
    #: supplies an object with those two methods; nothing about this service needs
    #: to know where the events go.
    audit_sink: Any = None,
    dlq_writer: DlqWriter | None = None,
    decision_store: ExtractionDecisionStore | None = None,
    canonical_facts: CanonicalFactWriter | None = None,
    commitments: CommitmentWriter | None = None,
) -> None:
    """Decode + validate one turn, run steward, apply fragments + KG, ack / nak / DLQ.

    Failure model:
      * ledger registration or a required projection fails → NAK / DLQ
      * deterministic redelivery resumes pending projections
      * a ledger-first privacy action is never applied to only one projection
    """
    stages = _TurnStages()
    deliveries = delivery_count(msg)
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        turn = parse_conversation_turn(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        log.error("turn_processor_bad_payload", error=str(exc))
        if dlq_writer is not None:
            await _record_dlq(
                dlq_writer,
                settings,
                msg,
                f"invalid turn payload: {exc}",
                delivery_count(msg),
            )
        await msg.ack()
        return

    # Cross-hop correlation id minted by the channel, carried on the envelope
    # (channel->agent->memory). Log it so one exchange is greppable end to end.
    trace_id = str(raw.get("trace_id") or "") if isinstance(raw, dict) else ""
    log.info(
        "turn_processor_ingest",
        trace_id=trace_id,
        turn_id=turn.turn_id,
        memory_space_id=turn.context.memory_space_id,
    )

    memory_space_id = turn.context.memory_space_id
    if expected_memory_space_id is not None and memory_space_id != expected_memory_space_id:
        log.error(
            "turn_processor_memory_space_mismatch",
            expected=expected_memory_space_id,
            got=memory_space_id,
            turn_id=turn.turn_id,
        )
        await msg.ack()
        return

    try:
        turn_audience = interaction_audience(turn.context)
    except MissingInteractionIdentity as exc:
        # Identity is part of the immutable message, so retry cannot repair it.
        # Preserve the rejected payload for diagnosis, then acknowledge without
        # ever running extraction or writing an Owner-visible fallback.
        log.error(
            "turn_processor_identity_missing",
            turn_id=turn.turn_id,
            memory_space_id=memory_space_id,
            error=str(exc),
        )
        await _record_dlq(dlq_writer, settings, msg, str(exc), deliveries)
        if audit_sink is not None:
            await audit_sink.record_rejected(
                turn, trace_id=trace_id, reason=str(exc), deliveries=deliveries
            )
        await msg.ack()
        return

    # ── decide ─────────────────────────────────────────────────────────────
    try:
        with stages.stage("steward"):
            decision, memory_intents = await _decide_once(
                steward,
                turn,
                decision_store,
            )
    except Exception as exc:
        metrics.TURNS_TOTAL.labels(outcome="steward_failed").inc()
        log.error(
            "turn_processor_steward_failed",
            error=str(exc),
            deliveries=deliveries,
            turn_id=turn.turn_id,
        )
        if deliveries >= max_deliveries:
            await _record_dlq(dlq_writer, settings, msg, str(exc), deliveries)
            if audit_sink is not None:
                await audit_sink.record_rejected(
                    turn, trace_id=trace_id, reason=str(exc), deliveries=deliveries
                )
            await msg.ack()
            log.error("turn_processor_dlq_ack", deliveries=deliveries)
        else:
            await msg.nak()
        return

    memory_intents = [
        intent.model_copy(update={"attributes": {**intent.attributes, "audience": turn_audience}})
        if intent.attributes.get("source_kind")
        in {
            "fragment",
            "triple",
            "invalidation",
        }
        else intent
        for intent in memory_intents
    ]
    privacy_action_count = len(decision.privacy_actions)

    turn_ts = turn.timestamp  # used as default valid_from / ended for triples

    # ── fragments + privacy (failure here NAKs — chroma is source of truth) ─
    fragments_written = 0
    try:
        # Privacy actions first; they may purge before we attempt new writes.
        if decision.privacy_actions:
            with stages.stage("privacy"):
                await _apply_privacy(
                    backend,
                    memory_space_id,
                    decision.privacy_actions,
                    interaction_readable_audiences(turn.context),
                    kg,
                    canonical_facts,
                    commitments,
                    decision_store,
                )
                if decision_store is None:
                    raise RuntimeError("privacy turns require the extraction decision ledger")
                await decision_store.redact_source_events(memory_space_id, [turn.turn_id])
            memory_intents = []
            decision = StewardDecision(
                should_write=False,
                reason="privacy action applied",
                produced_by="privacy:tombstone",
            )
        # Structured assertions project their own canonical drawer beside the
        # KG row below. Only fragment-only decisions are handled here; otherwise
        # writing the model's prose as another source creates two independently
        # correctable versions of the same fact.
        if decision.should_write and (not decision.triples or kg is None):
            with stages.stage("fragments"):
                if canonical_facts is None:
                    raise RuntimeError("natural long-term memory requires its fact ledger")
                fragment_intents = {
                    int(intent.attributes["source_index"]): intent
                    for intent in memory_intents
                    if intent.attributes.get("source_kind") == "fragment"
                }
                pending: list[tuple[MemoryFragment, Any]] = []
                for index, fragment in enumerate(decision.fragments):
                    intent = fragment_intents[index]
                    registration = await canonical_facts.register(intent, targets={"drawer"})
                    if registration.state != "active":
                        continue
                    projection_id = registration.projection_id or registration.assertion_id
                    if "drawer" not in registration.pending_targets:
                        existing_drawer = await backend.get_by_source_turn_id(
                            memory_space_id, f"canonical:{projection_id}"
                        )
                        if existing_drawer is not None:
                            continue
                        await canonical_facts.mark_projection_pending(
                            memory_space_id,
                            registration.assertion_id,
                            targets={"drawer"},
                        )
                    stamped = _stamped_for_turn(fragment, turn=turn, turn_ts=turn_ts)
                    pending.append(
                        (
                            _canonical_drawer(
                                stamped,
                                assertion_id=registration.assertion_id,
                                evidence_id=intent.intent_id,
                                projection_id=projection_id,
                            ),
                            registration,
                        )
                    )
                await ingest_memory_fragments(backend, [item[0] for item in pending])
                for _, registration in pending:
                    await canonical_facts.mark_projected(
                        memory_space_id,
                        registration.assertion_id,
                        targets={"drawer"},
                    )
                fragments_written = len(pending)
    except Exception as exc:
        log.error(
            "turn_processor_fragment_failed",
            error=str(exc),
            deliveries=deliveries,
            turn_id=turn.turn_id,
        )
        if deliveries >= max_deliveries:
            await _record_dlq(dlq_writer, settings, msg, str(exc), deliveries)
            if audit_sink is not None:
                await audit_sink.record_rejected(
                    turn, trace_id=trace_id, reason=str(exc), deliveries=deliveries
                )
            await msg.ack()
            log.error("turn_processor_dlq_ack", deliveries=deliveries)
        else:
            await msg.nak()
        return

    # ── KG writes ──────────────────────────────────────────────────────────
    kg_triples_added = 0
    kg_invalidations_applied = 0
    mentions_written = 0
    mentions_rejected = 0
    kg_skipped_low_confidence = 0
    kg_exact_noop = 0
    kg_failures: list[str] = []
    canonical_projection_failures: list[str] = []
    min_conf = settings.kg.min_confidence_to_write if kg is not None else 1.0

    if kg is not None:
        with stages.stage("kg"):
            # Invalidations first so a "change of mind" turn always ends the old
            # fact before any new one referencing the same (s,p,o) shape lands.
            invalidation_intents: dict[int, MemoryIntent] = {}
            for intent in memory_intents:
                source_index = intent.attributes.get("source_index")
                if (
                    intent.attributes.get("source_kind") == "invalidation"
                    and isinstance(source_index, int)
                    and not isinstance(source_index, bool)
                ):
                    invalidation_intents[source_index] = intent
            for index, inv in enumerate(decision.invalidations):
                intent = invalidation_intents.get(index)
                audience = turn_audience
                try:
                    if canonical_facts is not None and intent is not None:
                        if intent.occurred_at is None:
                            intent = intent.model_copy(update={"occurred_at": turn_ts})
                        result = await invalidate_exact_canonical_fact(
                            backend,
                            kg,
                            intent,
                            canonical_facts,
                        )
                        rows = result.kg_rows_invalidated
                    else:
                        rows = await kg.invalidate(
                            subject=inv.subject,
                            predicate=inv.predicate,
                            object=inv.object,
                            audiences=(audience,),
                            ended=inv.ended or turn_ts,
                        )
                    if rows > 0:
                        kg_invalidations_applied += 1
                    else:
                        log.info(
                            "kg_invalidate_no_match",
                            subject=inv.subject,
                            predicate=inv.predicate,
                            object=inv.object,
                        )
                except Exception as exc:
                    kg_failures.append(f"inv:{exc}")
                    if (
                        canonical_facts is not None
                        and decision_store is not None
                        and intent is not None
                    ):
                        canonical_projection_failures.append(str(exc))
                    log.warning("kg_invalidate_failed", error=str(exc))

            triple_intents: dict[int, MemoryIntent] = {}
            for intent in memory_intents:
                source_index = intent.attributes.get("source_index")
                if (
                    intent.attributes.get("source_kind") == "triple"
                    and isinstance(source_index, int)
                    and not isinstance(source_index, bool)
                ):
                    triple_intents[source_index] = intent
            for index, t in enumerate(decision.triples):
                if t.confidence < min_conf:
                    kg_skipped_low_confidence += 1
                    continue
                try:
                    intent = triple_intents.get(index)
                    audience = turn_audience
                    if canonical_facts is None or intent is None:
                        raise RuntimeError("structured natural memory requires its fact ledger")
                    exact = await canonical_facts.get_fact(
                        intent.memory_space_id,
                        audience,
                        intent.subject or "",
                        intent.predicate or "",
                        intent.object or "",
                    )
                    if exact is not None and exact.state != "active":
                        # A current fact may become true again after an earlier
                        # correction. Reuse the canonical ledger's existing
                        # reactivation transition rather than either rejecting
                        # the new evidence forever or creating a second fact id.
                        # The steward has already supplied a complete,
                        # high-confidence triple; this is not inferred from the
                        # fragment text.
                        intent = intent.model_copy(
                            update={
                                "operation_hint": "update",
                                "attributes": {
                                    **intent.attributes,
                                    "reason": "steward reasserted exact fact",
                                },
                            }
                        )
                        registration = await canonical_facts.register_reactivation(
                            intent,
                            targets={"drawer", "kg"},
                        )
                    else:
                        registration = await canonical_facts.register(
                            intent,
                            targets={"drawer", "kg"},
                        )
                    if registration.state != "active" and not registration.reactivation_pending:
                        kg_exact_noop += 1
                        continue
                    projection_id = registration.projection_id or registration.assertion_id

                    drawer_pending = "drawer" in registration.pending_targets
                    if not drawer_pending or not registration.evidence_created:
                        existing_drawer = await backend.get_by_source_turn_id(
                            memory_space_id, f"canonical:{projection_id}"
                        )
                        if existing_drawer is not None:
                            if drawer_pending:
                                await canonical_facts.mark_projected(
                                    memory_space_id,
                                    registration.assertion_id,
                                    targets={"drawer"},
                                )
                            drawer_pending = False
                        elif not drawer_pending:
                            await canonical_facts.mark_projection_pending(
                                memory_space_id,
                                registration.assertion_id,
                                targets={"drawer"},
                            )
                            drawer_pending = True
                    if drawer_pending:
                        await ingest_memory_fragment(
                            backend,
                            _drawer_for_triple(
                                t,
                                turn=turn,
                                turn_ts=turn_ts,
                                assertion_id=registration.assertion_id,
                                evidence_id=intent.intent_id,
                                projection_id=projection_id,
                            ),
                        )
                        await canonical_facts.mark_projected(
                            memory_space_id,
                            registration.assertion_id,
                            targets={"drawer"},
                        )
                        fragments_written += 1

                    kg_pending = "kg" in registration.pending_targets
                    should_verify = (
                        not kg_pending
                        or not registration.evidence_created
                        or registration.evidence_count > 1
                    )
                    if should_verify:
                        if await _canonical_kg_visible(kg, intent):
                            if kg_pending:
                                await canonical_facts.mark_projected(
                                    memory_space_id,
                                    registration.assertion_id,
                                    targets={"kg"},
                                )
                            if registration.reactivation_pending:
                                await canonical_facts.mark_reactivated(
                                    memory_space_id,
                                    intent.intent_id,
                                    targets={"drawer", "kg"},
                                )
                            kg_exact_noop += 1
                            continue
                        if not kg_pending:
                            await canonical_facts.mark_projection_pending(
                                memory_space_id,
                                registration.assertion_id,
                                targets={"kg"},
                            )
                    await kg.add_triple(
                        audience=audience,
                        subject=t.subject,
                        predicate=t.predicate,
                        object=t.object,
                        valid_from=t.valid_from or turn_ts,
                        valid_to=t.valid_to,
                        confidence=t.confidence,
                        source_turn_id=(f"canonical:{projection_id}"),
                        assertion_id=registration.assertion_id,
                        evidence_id=intent.intent_id,
                        projection_id=projection_id,
                        adapter_name="steward-llm",
                    )
                    await canonical_facts.mark_projected(
                        memory_space_id,
                        registration.assertion_id,
                        targets={"kg"},
                    )
                    if registration.reactivation_pending:
                        await canonical_facts.mark_reactivated(
                            memory_space_id,
                            intent.intent_id,
                            targets={"drawer", "kg"},
                        )
                    kg_triples_added += 1
                except Exception as exc:
                    kg_failures.append(f"add:{exc}")
                    canonical_projection_failures.append(str(exc))
                    log.warning("kg_add_triple_failed", error=str(exc))

            # Phase 3 — entity_mentions write. Runs AFTER triples so the
            # anti-hallucination guard can verify each mention's entity_id was
            # actually asserted in this turn (steward output is LLM-derived,
            # so cross-validation with structured triple ids is essential).
            mentions_written, mentions_rejected = await _write_mentions(
                kg,
                decision,
                audience=turn_audience,
                kg_failures=kg_failures,
            )

    # A turn that reached here was absorbed. "wrote" versus "skipped" is the
    # distinction that matters: a steady stream of skipped turns is either a
    # quiet conversation or a broken classifier, and the ratio is what tells
    # them apart.
    metrics.TURNS_TOTAL.labels(outcome="wrote" if decision.should_write else "skipped").inc()

    # G8: one structured line per turn — operators can grep this without
    # parsing the whole log stream.
    #
    # The ``*_ms`` fields say which stage owned this turn. They are here rather
    # than only in the histogram because an end-to-end latency outlier is
    # always about one turn, and a percentile cannot name one.
    stages.observe_total()
    log.info(
        "turn_processed",
        turn_id=turn.turn_id,
        memory_space_id=memory_space_id,
        device_id=turn.context.device_id,
        session_id=turn.context.session_id,
        should_write=decision.should_write,
        fragments=fragments_written,
        triples=kg_triples_added,
        invalidations=kg_invalidations_applied,
        kg_skipped_lowconf=kg_skipped_low_confidence,
        kg_exact_noop=kg_exact_noop,
        kg_failures=len(kg_failures),
        kg_failure_sample=kg_failures[:2],
        canonical_projection_failures=len(canonical_projection_failures),
        privacy_actions=privacy_action_count,
        mentions=mentions_written if kg is not None else 0,
        mentions_rejected=mentions_rejected if kg is not None else 0,
        **stages.log_fields(),
    )
    if canonical_projection_failures:
        error = "canonical projection failed: " + "; ".join(canonical_projection_failures[:2])
        if deliveries >= max_deliveries:
            await _record_dlq(dlq_writer, settings, msg, error, deliveries)
            if audit_sink is not None:
                await audit_sink.record_rejected(
                    turn,
                    trace_id=trace_id,
                    reason=error,
                    deliveries=deliveries,
                )
            await msg.ack()
            log.error(
                "turn_processor_canonical_invalidation_dlq_ack",
                deliveries=deliveries,
                error=error,
            )
        else:
            await msg.nak()
            log.warning(
                "turn_processor_canonical_invalidation_nak",
                deliveries=deliveries,
                error=error,
            )
        return
    if audit_sink is not None:
        await audit_sink.record_absorbed(
            turn,
            trace_id=trace_id,
            should_write=decision.should_write,
            fragments=fragments_written,
            triples=kg_triples_added,
        )
    await msg.ack()


async def _canonical_kg_visible(kg: Any, intent: MemoryIntent) -> bool:
    """Verify that an assertion marked projected still has an active KG row."""
    rows = await kg.query_entity(
        intent.subject,
        audiences=(str(intent.attributes["audience"]),),
        direction="outgoing",
        include_sensitive=True,
    )
    return any(
        row.subject == intent.subject
        and row.predicate == intent.predicate
        and row.object == intent.object
        for row in rows
    )


async def _write_mentions(
    kg: Any,
    decision: Any,
    *,
    audience: str,
    kg_failures: list[str],
) -> tuple[int, int]:
    """Persist ``decision.mentions`` to the KG, dropping LLM hallucinations.

    Anti-hallucination guard: a steward (LLM) may emit ``EntityMention``
    pointing at an entity_id that was never asserted in this turn's
    ``triples``. We compute the union of subjects/objects in the turn's
    triples and reject any mention outside that set — without this, we'd
    accumulate alias rows for entities that don't actually exist in the KG.

    G7 semantics: per-mention failures are logged + counted in ``kg_failures``
    but never raise into the caller — turn ack proceeds regardless.

    Returns ``(written, rejected)``.
    """
    mentions = list(getattr(decision, "mentions", None) or [])
    if not mentions:
        return 0, 0

    # Entities asserted by this turn = the only entity_ids we'll accept
    # mentions for. Mentions referencing unrelated entity_ids are LLM noise.
    triple_entities: set[str] = set()
    for t in decision.triples or []:
        if t.subject:
            triple_entities.add(t.subject)
        if t.object:
            triple_entities.add(t.object)

    written = 0
    rejected = 0
    for m in mentions:
        if m.entity_id not in triple_entities:
            rejected += 1
            log.warning(
                "kg_mention_rejected_unknown_entity",
                entity=m.entity_id,
                alias=m.alias,
            )
            continue
        try:
            await kg.record_entity_mention(
                entity_id=m.entity_id,
                alias=m.alias,
                audience=audience,
                source="steward-llm",
                confidence=m.confidence,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - G7: never abort turn ack
            kg_failures.append(f"mention:{exc}")
            log.warning(
                "kg_mention_write_failed",
                entity=m.entity_id,
                alias=m.alias,
                error=str(exc),
            )
    return written, rejected


async def process_command_message(
    msg: Any,
    *,
    backend: Any,
    kg: Any,
    settings: MemorySettings,
    expected_memory_space_id: str | None = None,
    command_status: CommandStatusWriter | None = None,
    dlq_writer: DlqWriter | None = None,
    canonical_facts: CanonicalFactWriter | None = None,
    commitments: CommitmentWriter | None = None,
    decision_store: ExtractionDecisionStore | None = None,
) -> None:
    """Handle ``MemoryCommandPayload`` from ``eidolon.memory.cmd.<memory_space_token>``.

    ACK means the command was applied (or reached a recorded terminal failure).
    Transient failures are NAKed so explicit user/admin intent is not silently
    lost. The command-status projection is deliberately separate from the main
    memory backend, keeping status reads off the Chroma/KG critical section.
    """
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        cmd: MemoryCommandPayload = parse_memory_command(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        log.error("cmd_bad_payload", error=str(exc))
        if dlq_writer is not None:
            await _record_dlq(
                dlq_writer,
                settings,
                msg,
                f"invalid command payload: {exc}",
                delivery_count(msg),
            )
        await msg.ack()
        return

    if expected_memory_space_id is not None and cmd.memory_space_id != expected_memory_space_id:
        log.error(
            "cmd_memory_space_mismatch",
            expected=expected_memory_space_id,
            got=cmd.memory_space_id,
            request_id=cmd.request_id,
        )
        await _record_command_status(
            command_status,
            "failed",
            cmd.request_id,
            kind=cmd.kind,
            error="memory_space mismatch",
        )
        await msg.ack()
        return

    status_get = getattr(command_status, "get", None)
    if status_get is not None:
        existing_status = await status_get(cmd.request_id)
        if existing_status is not None and existing_status.status == "applied":
            log.info(
                "cmd_terminal_redelivery_ack",
                request_id=cmd.request_id,
                status=existing_status.status,
            )
            await msg.ack()
            return
        # A failed command is intentionally retryable only after an explicit
        # republish (for example the operator DLQ replay). JetStream already
        # ACKed its terminal delivery, so this cannot create an automatic loop;
        # keeping failed → applied possible is the recovery contract recorded
        # by CommandStatusLedger.

    if kg is None and isinstance(cmd, (KgAddTripleCommand, KgInvalidateCommand)):
        # The graph is switched off for this deployment. Say so, rather than
        # leaving the caller to time out waiting for a terminal status.
        log.info(
            "cmd_kg_not_configured",
            request_id=cmd.request_id,
            kind=cmd.kind,
        )
        await _record_command_status(
            command_status,
            "failed",
            cmd.request_id,
            kind=cmd.kind,
            error="kg_not_configured",
        )
        await msg.ack()
        return

    try:
        resource_id: str | None = None
        if isinstance(cmd, KgAddTripleCommand):
            triple_id = await kg.add_triple(
                audience=OWNER_AUDIENCE,
                subject=cmd.subject,
                predicate=cmd.predicate,
                object=cmd.object,
                valid_from=cmd.valid_from,
                valid_to=cmd.valid_to,
                confidence=cmd.confidence,
                source_turn_id=cmd.source_drawer_id or f"req:{cmd.request_id}",
                adapter_name=cmd.adapter_name,
            )
            log.info(
                "cmd_kg_add_ok",
                request_id=cmd.request_id,
                triple_id=triple_id,
                subject=cmd.subject,
                predicate=cmd.predicate,
                object=cmd.object,
            )
            resource_id = str(triple_id)
        elif isinstance(cmd, KgInvalidateCommand):
            rows = await kg.invalidate(
                subject=cmd.subject,
                predicate=cmd.predicate,
                object=cmd.object,
                audiences=(OWNER_AUDIENCE,),
                ended=cmd.ended,
            )
            if rows == 0:
                already_applied = await kg.find_invalidation_applied(
                    cmd.subject,
                    cmd.predicate,
                    cmd.object,
                    cmd.ended,
                )
                if not already_applied:
                    raise LookupError("no matching triple to invalidate")
            log.info(
                "cmd_kg_invalidate_ok",
                request_id=cmd.request_id,
                rows=rows,
                subject=cmd.subject,
                predicate=cmd.predicate,
                object=cmd.object,
            )
            resource_id = f"invalidated:{rows}"
        elif isinstance(cmd, ConsolidatorIngestThemeCommand):
            resource_id = await _ingest_theme(backend, cmd)
            log.info(
                "cmd_theme_ingest_ok",
                request_id=cmd.request_id,
                underlying_wing=cmd.underlying_wing,
                drawer_count=len(cmd.source_drawer_ids),
                confidence=cmd.confidence,
            )
        elif isinstance(cmd, MemoryIntentCommand):
            resource_id = await apply_explicit_intent(
                backend,
                kg,
                cmd,
                canonical_facts=canonical_facts,
                commitments=commitments,
            )
            log.info(
                "cmd_memory_intent_ok",
                request_id=cmd.request_id,
                intent_id=cmd.intent.intent_id,
                intent_type=cmd.intent.intent_type,
                authority=cmd.intent.authority,
            )
        elif isinstance(cmd, PrivacyMutationCommand):
            # Both stores, because a person forgetting something means the thing
            # and not the copy of it that happens to live in Chroma. Until
            # 2026-08-06 only the drawer went: the triples stayed and kept being
            # rendered into the next prompt, so the product said yes and then
            # produced the fact it had just agreed to forget.
            #
            # Before the vector mutation, deliberately — see the helper.
            changed, forgotten = await forget_resolved_projections(
                backend,
                kg,
                canonical_facts,
                commitments,
                decision_store,
                cmd.memory_space_id,
                drawer_ids=cmd.drawer_ids,
                commitment_ids=cmd.commitment_ids,
                source_event_ids=cmd.source_event_ids,
                hard=cmd.action == "delete",
            )
            resource_id = (
                f"{cmd.action}:"
                f"{len(cmd.drawer_ids) + len(cmd.commitment_ids) + len(cmd.source_event_ids)}:"
                f"{cmd.preview_id}"
            )
            log.info(
                "cmd_privacy_mutation_ok",
                request_id=cmd.request_id,
                preview_id=cmd.preview_id,
                action=cmd.action,
                drawer_count=len(changed),
                source_event_count=len(cmd.source_event_ids),
                # Separate from ``drawer_count`` because they answer different
                # questions and their ratio is the interesting one: turns with no
                # triples are ordinary, but a forget that touched drawers and no
                # statements on a graph-enabled space is worth looking at.
                kg_statements_forgotten=forgotten,
            )
        elif isinstance(cmd, DeviceSyncBatchPayload):
            log.info(
                "cmd_device_sync_batch_ignored_on_cmd_subject",
                request_id=cmd.request_id,
                events=len(cmd.events),
            )
            await _record_command_status(
                command_status,
                "failed",
                cmd.request_id,
                kind=cmd.kind,
                error="device_sync_batch must use the sync subject",
            )
            await msg.ack()
            return
    except MemoryIntentRejected as exc:
        log.warning(
            "cmd_memory_intent_rejected",
            request_id=cmd.request_id,
            error=str(exc),
        )
        await _record_command_status(
            command_status,
            "failed",
            cmd.request_id,
            kind=cmd.kind,
            error=str(exc),
        )
        await msg.ack()
        return
    except Exception as exc:
        deliveries = delivery_count(msg)
        log.error(
            "cmd_apply_failed",
            request_id=cmd.request_id,
            error=str(exc),
            deliveries=deliveries,
        )
        if deliveries >= settings.nats.worker_max_deliveries:
            await _record_command_status(
                command_status,
                "failed",
                cmd.request_id,
                kind=cmd.kind,
                error=str(exc),
            )
            await _record_dlq(dlq_writer, settings, msg, str(exc), deliveries)
            await msg.ack()
            log.error("cmd_dlq_ack", request_id=cmd.request_id, deliveries=deliveries)
        else:
            await _record_command_status(
                command_status,
                "retrying",
                cmd.request_id,
                kind=cmd.kind,
                error=str(exc),
            )
            await msg.nak()
        return

    await _record_command_status(
        command_status,
        "applied",
        cmd.request_id,
        kind=cmd.kind,
        resource_id=resource_id,
    )
    await msg.ack()


async def _record_command_status(
    ledger: CommandStatusWriter | None,
    transition: CommandStatus,
    request_id: str,
    *,
    kind: str,
    resource_id: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort projection update; never make a durable write look failed."""
    if ledger is None:
        return
    try:
        if transition == "applied":
            await ledger.record_applied(
                request_id,
                kind=kind,
                resource_id=resource_id,
            )
        elif transition == "retrying":
            await ledger.record_retrying(
                request_id,
                kind=kind,
                error=error or "command apply failed",
            )
        elif transition == "failed":
            await ledger.record_failed(
                request_id,
                kind=kind,
                error=error or "command failed",
            )
        else:
            await ledger.record_accepted(request_id, kind=kind)
    except Exception as exc:  # noqa: BLE001 - projection cannot own write outcome
        log.error(
            "command_status_projection_failed",
            request_id=request_id,
            transition=transition,
            error=str(exc),
        )


async def process_sync_message(
    msg: Any,
    *,
    steward: StewardProtocol,
    backend: Any,
    ledger: Any,
    settings: MemorySettings,
    expected_memory_space_id: str,
    decision_store: ExtractionDecisionStore | None = None,
    kg: Any = None,
    canonical_facts: CanonicalFactWriter | None = None,
    commitments: CommitmentWriter | None = None,
) -> None:
    """Handle ``DeviceSyncBatchPayload`` from ``eidolon.memory.sync.<memory_space_token>``."""
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        command = parse_memory_command(raw)
        if not isinstance(command, DeviceSyncBatchPayload):
            raise ValueError("sync payload must be device_sync_batch")
        batch = command
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        log.error("sync_bad_payload", error=str(exc))
        await msg.ack()
        return

    if batch.memory_space_id != expected_memory_space_id:
        log.error(
            "sync_memory_space_mismatch",
            expected=expected_memory_space_id,
            got=batch.memory_space_id,
            request_id=batch.request_id,
        )
        await msg.ack()
        return

    synced = 0
    skipped = 0
    failed = 0
    for event in batch.events:
        if await ledger.seen(
            event_id=event.event_id,
            idempotency_hash=event.idempotency_hash,
        ):
            skipped += 1
            continue
        try:
            turn = ConversationTurnPayload.model_validate(event.turn)
            if turn.context.memory_space_id != expected_memory_space_id:
                raise ValueError("sync event turn memory_space_id mismatch")
            if turn.context.device_id != batch.device_id:
                raise ValueError("sync event turn device_id mismatch")

            # Offline delivery is only a transport variant of an ordinary turn.
            # Feed it through the exact same ledger/outbox processor so sync can
            # never become a second fact source or skip KG/privacy projections.
            turn_msg = _SyncTurnMessage(turn)
            observer = _SyncTurnObserver()
            await process_turn_message(
                turn_msg,
                steward=steward,
                backend=backend,
                kg=kg,
                settings=settings,
                max_deliveries=settings.nats.worker_max_deliveries,
                expected_memory_space_id=expected_memory_space_id,
                audit_sink=observer,
                decision_store=decision_store,
                canonical_facts=canonical_facts,
                commitments=commitments,
            )
            if turn_msg.nacked or not observer.absorbed:
                reason = observer.rejection_reason or "turn projection was not absorbed"
                raise RuntimeError(reason)
            await ledger.mark_synced(
                event_id=event.event_id,
                device_id=batch.device_id,
                instance_id=batch.instance_id,
                turn_id=turn.turn_id,
                idempotency_hash=event.idempotency_hash,
            )
            synced += 1
        except Exception as exc:  # noqa: BLE001 - one bad offline event should not block batch
            failed += 1
            log.warning("sync_event_failed", event_id=event.event_id, error=str(exc))

    log.info(
        "sync_batch_processed",
        request_id=batch.request_id,
        memory_space_id=batch.memory_space_id,
        device_id=batch.device_id,
        synced=synced,
        skipped=skipped,
        failed=failed,
    )
    await msg.ack()


class _SyncTurnMessage:
    """Minimal NATS message facade for the shared turn processor."""

    def __init__(self, turn: ConversationTurnPayload) -> None:
        envelope = envelope_memory_payload(turn, trace_id=turn.turn_id)
        self.data = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
        self.subject = "eidolon.memory.sync.turn"
        self.metadata = None
        self.acked = False
        self.nacked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.nacked = True


class _SyncTurnObserver:
    """Distinguish absorbed turns from terminal rejections on an ACK."""

    def __init__(self) -> None:
        self.absorbed = False
        self.rejection_reason = ""

    async def record_absorbed(self, *_args: Any, **_kwargs: Any) -> None:
        self.absorbed = True

    async def record_rejected(self, *_args: Any, **kwargs: Any) -> None:
        self.rejection_reason = str(kwargs.get("reason") or "turn rejected")


async def _ingest_theme(backend: Any, cmd: ConsolidatorIngestThemeCommand) -> str:
    """Write a consolidator-produced theme directly as a Wing_Theme fragment.

    Skip the steward layer entirely — themes are already a steward output
    (synthesized by ``eidolon-memory-consolidator``), and putting them
    through ``LiteLLMSteward.decide()`` again would either:
      a) produce theme-of-theme noise, or
      b) be a wasted LLM round-trip.

    Idempotency: the deterministic ``key`` derived from ``cmd.request_id``
    means re-delivery of the same theme collapses to one drawer at the
    chroma layer.
    """
    fragment = MemoryFragment(
        memory_id=f"theme:{cmd.request_id}",
        memory_space_id=cmd.memory_space_id,
        memory_realm_id=cmd.memory_space_id,
        scope="persona",
        visibility="all_devices",
        source_device_id="system",
        target_device_id=None,
        source_instance_id="consolidator",
        wing="Wing_Theme",
        room=f"theme:{cmd.request_id[:16]}",
        content=cmd.text,
        memory_type="profile",  # closest existing type for high-level summaries
        importance=4,
        confidence=cmd.confidence,
        occurred_at=cmd.issued_at,
        source_turn_id=f"consolidator:{cmd.request_id}",
        session_id="consolidator",
        tags=["theme", cmd.underlying_wing],
        audience=cmd.audience,
        privacy="normal",
        metadata={
            "source": "consolidator",
            "underlying_wing": cmd.underlying_wing,
            "window_days": cmd.window_days,
            "source_drawer_ids": cmd.source_drawer_ids,
        },
    )
    await ingest_memory_fragment(backend, fragment)
    return fragment.memory_id
