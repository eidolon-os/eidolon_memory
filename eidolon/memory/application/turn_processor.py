"""Process JetStream messages: ConversationTurn + MemoryCommand (KG plan §4.4).

Used by ``agent_runner``'s in-process NATS subscriber. Steward runs in-process
returning a ``StewardDecision``; this module is responsible for applying that
decision to chroma drawers + KG triples + privacy actions, with the right
fail-mode for each (G7 KG failure does not block chat ack).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

from eidolon_sdk.memory import (
    ConsolidatorIngestThemeCommand,
    ConversationTurnPayload,
    DeviceSyncBatchPayload,
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryCommandPayload,
    MemoryIntent,
    MemoryIntentCommand,
    PrivacyMutationCommand,
    parse_conversation_turn,
    parse_memory_command,
)
from pydantic import ValidationError

from eidolon.memory.application.canonical_invalidation import (
    invalidate_exact_canonical_fact,
)
from eidolon.memory.application.explicit_intents import (
    MemoryIntentRejected,
    apply_explicit_intent,
)
from eidolon.memory.application.forget import archive_exact_drawers, delete_exact_drawers
from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.application.memory_intents import memory_intents_from_decision
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
    DlqWriter,
    ExtractionDecisionStore,
)
from eidolon.memory.domain.steward import StewardDecision
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


async def _apply_privacy(backend: Any, memory_space_id: str, actions: list) -> None:
    """Wrapper for the steward's privacy-action handler (delete / archive)."""
    if not actions:
        return
    await apply_privacy_actions(
        backend,
        memory_space_id=memory_space_id,
        actions=actions,
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
    audit_sink: Any = None,
    dlq_writer: DlqWriter | None = None,
    decision_store: ExtractionDecisionStore | None = None,
    canonical_facts: CanonicalFactWriter | None = None,
) -> None:
    """Decode + validate one turn, run steward, apply fragments + KG, ack / nak / DLQ.

    Failure model (G7 from KG plan §4.4):
      * fragment write fails → NAK / DLQ (chroma is source of truth for chat)
      * KG write fails → log + ack (KG is incremental; chat conversation must not stall)
      * privacy-action fails → log + ack (don't redeliver delete requests)
    """
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
    if (
        expected_memory_space_id is not None
        and memory_space_id != expected_memory_space_id
    ):
        log.error(
            "turn_processor_memory_space_mismatch",
            expected=expected_memory_space_id,
            got=memory_space_id,
            turn_id=turn.turn_id,
        )
        await msg.ack()
        return

    # ── working memory: append BEFORE steward (G7 ordering) ────────────────
    # Steward failures must not lose the raw turn from the ring — short-term
    # continuity is independent of extraction quality. ``working_memory`` is
    # ``None`` on backends that opt out (test fakes); the ring's own
    # ``append`` is a no-op when ``maxlen=0``, so this branch is safe.
    ring = getattr(backend, "working_memory", None)
    if ring is not None:
        try:
            await ring.append(turn)
        except Exception as exc:  # noqa: BLE001 - defensive: never break turn ack
            log.warning("working_memory_append_failed", error=str(exc))

    # ── decide ─────────────────────────────────────────────────────────────
    try:
        decision, memory_intents = await _decide_once(
            steward,
            turn,
            decision_store,
        )
    except Exception as exc:
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

    turn_ts = turn.timestamp  # used as default valid_from / ended for triples

    # ── fragments + privacy (failure here NAKs — chroma is source of truth) ─
    fragments_written = 0
    try:
        # Privacy actions first; they may purge before we attempt new writes.
        await _apply_privacy(backend, memory_space_id, decision.privacy_actions)
        if decision.should_write:
            for fragment in decision.fragments:
                fragment = stamp_fragment_identity(
                    fragment,
                    context=turn.context,
                    source_turn_id=turn.turn_id,
                )
                stamped = (
                    fragment
                    if fragment.occurred_at
                    else fragment.model_copy(update={"occurred_at": turn_ts})
                )
                await ingest_memory_fragment(backend, stamped)
                fragments_written += 1
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

    # ── KG writes (G7: failure logged, never NAK) ──────────────────────────
    kg_triples_added = 0
    kg_invalidations_applied = 0
    mentions_written = 0
    mentions_rejected = 0
    kg_skipped_low_confidence = 0
    kg_exact_noop = 0
    kg_failures: list[str] = []
    min_conf = settings.kg.min_confidence_to_write if kg is not None else 1.0

    if kg is not None:
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
            try:
                intent = invalidation_intents.get(index)
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
                registration = None
                if canonical_facts is not None and intent is not None:
                    registration = await canonical_facts.register(
                        intent,
                        targets={"kg"},
                    )
                    if registration.state == "invalidated":
                        kg_exact_noop += 1
                        continue
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
                            kg_exact_noop += 1
                            continue
                        if not kg_pending:
                            await canonical_facts.mark_projection_pending(
                                memory_space_id,
                                registration.assertion_id,
                                targets={"kg"},
                            )
                await kg.add_triple(
                    subject=t.subject,
                    predicate=t.predicate,
                    object=t.object,
                    valid_from=t.valid_from or turn_ts,
                    valid_to=t.valid_to,
                    confidence=t.confidence,
                    source_turn_id=(
                        f"canonical:{registration.assertion_id}:"
                        f"evidence:{intent.intent_id}"
                        if registration is not None and intent is not None
                        else turn.turn_id
                    ),
                    adapter_name="steward-llm",
                )
                if registration is not None:
                    await canonical_facts.mark_projected(
                        memory_space_id,
                        registration.assertion_id,
                        targets={"kg"},
                    )
                kg_triples_added += 1
            except Exception as exc:
                kg_failures.append(f"add:{exc}")
                log.warning("kg_add_triple_failed", error=str(exc))

        # Phase 3 — entity_mentions write. Runs AFTER triples so the
        # anti-hallucination guard can verify each mention's entity_id was
        # actually asserted in this turn (steward output is LLM-derived,
        # so cross-validation with structured triple ids is essential).
        mentions_written, mentions_rejected = await _write_mentions(
            kg, decision, kg_failures=kg_failures,
        )

    # G8: one structured line per turn — operators can grep this without
    # parsing the whole log stream.
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
        privacy_actions=len(decision.privacy_actions),
        mentions=mentions_written if kg is not None else 0,
        mentions_rejected=mentions_rejected if kg is not None else 0,
    )
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
    for t in (decision.triples or []):
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
                entity=m.entity_id, alias=m.alias,
            )
            continue
        try:
            await kg.record_entity_mention(
                entity_id=m.entity_id,
                alias=m.alias,
                source="steward-llm",
                confidence=m.confidence,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - G7: never abort turn ack
            kg_failures.append(f"mention:{exc}")
            log.warning(
                "kg_mention_write_failed",
                entity=m.entity_id, alias=m.alias, error=str(exc),
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

    if (
        expected_memory_space_id is not None
        and cmd.memory_space_id != expected_memory_space_id
    ):
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

    try:
        resource_id: str | None = None
        if isinstance(cmd, KgAddTripleCommand):
            triple_id = await kg.add_triple(
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
            )
            log.info(
                "cmd_memory_intent_ok",
                request_id=cmd.request_id,
                intent_id=cmd.intent.intent_id,
                intent_type=cmd.intent.intent_type,
                authority=cmd.intent.authority,
            )
        elif isinstance(cmd, PrivacyMutationCommand):
            if cmd.action == "delete":
                changed = await delete_exact_drawers(
                    backend, cmd.memory_space_id, cmd.drawer_ids
                )
            else:
                changed = await archive_exact_drawers(
                    backend, cmd.memory_space_id, cmd.drawer_ids
                )
            resource_id = f"{cmd.action}:{len(changed)}:{cmd.preview_id}"
            log.info(
                "cmd_privacy_mutation_ok",
                request_id=cmd.request_id,
                preview_id=cmd.preview_id,
                action=cmd.action,
                drawer_count=len(changed),
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
) -> None:
    """Handle ``DeviceSyncBatchPayload`` from ``eidolon.memory.sync.<memory_space_token>``."""
    del settings
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
        if ledger.seen(
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

            ring = getattr(backend, "working_memory", None)
            if ring is not None:
                await ring.append(turn)

            decision, _memory_intents = await _decide_once(
                steward,
                turn,
                decision_store,
            )
            await _apply_privacy(
                backend,
                expected_memory_space_id,
                decision.privacy_actions,
            )
            for fragment in decision.fragments if decision.should_write else []:
                stamped = (
                    fragment
                    if fragment.occurred_at
                    else fragment.model_copy(update={"occurred_at": turn.timestamp})
                )
                await ingest_memory_fragment(backend, stamped)
            ledger.mark_synced(
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
        memory_type="profile",   # closest existing type for high-level summaries
        importance=4,
        confidence=cmd.confidence,
        occurred_at=cmd.issued_at,
        source_turn_id=f"consolidator:{cmd.request_id}",
        session_id="consolidator",
        tags=["theme", cmd.underlying_wing],
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
