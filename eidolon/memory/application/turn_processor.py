"""Process JetStream messages: ConversationTurn + MemoryCommand (KG plan §4.4).

Used by ``agent_runner``'s in-process NATS subscriber. Steward runs in-process
returning a ``StewardDecision``; this module is responsible for applying that
decision to chroma drawers + KG triples + privacy actions, with the right
fail-mode for each (G7 KG failure does not block chat ack).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError
from eidolon_sdk.memory import (
    USER_CONFIRMED_ROOM_PREFIX,
    ConversationTurnPayload,
    ConsolidatorIngestThemeCommand,
    DeviceSyncBatchPayload,
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryCommandPayload,
    UserConfirmedFactCommand,
)

from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.application.steward.common import (
    apply_privacy_actions,
    stamp_fragment_identity,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class StewardProtocol(Protocol):
    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision: ...


def append_dlq(settings: MemorySettings, payload: bytes, error: str, deliveries: int) -> None:
    """Append a poison message + error to ``settings.nats.dlq_log_path`` (one JSON per line)."""
    path = Path(settings.nats.dlq_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "deliveries": deliveries,
        "payload_preview": payload[:500].decode("utf-8", errors="replace"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
        turn = ConversationTurnPayload.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        log.error("turn_processor_bad_payload", error=str(exc))
        await msg.ack()
        return

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
        decision = await steward.decide(turn)
    except Exception as exc:
        log.error(
            "turn_processor_steward_failed",
            error=str(exc),
            deliveries=deliveries,
            turn_id=turn.turn_id,
        )
        if deliveries >= max_deliveries:
            append_dlq(settings, msg.data, str(exc), deliveries)
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
            append_dlq(settings, msg.data, str(exc), deliveries)
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
    kg_failures: list[str] = []
    min_conf = settings.kg.min_confidence_to_write if kg is not None else 1.0

    if kg is not None:
        # Invalidations first so a "change of mind" turn always ends the old
        # fact before any new one referencing the same (s,p,o) shape lands.
        for inv in decision.invalidations:
            try:
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

        for t in decision.triples:
            if t.confidence < min_conf:
                kg_skipped_low_confidence += 1
                continue
            try:
                await kg.add_triple(
                    subject=t.subject,
                    predicate=t.predicate,
                    object=t.object,
                    valid_from=t.valid_from or turn_ts,
                    valid_to=t.valid_to,
                    confidence=t.confidence,
                    source_turn_id=turn.turn_id,
                    adapter_name="steward-llm",
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
        kg_failures=len(kg_failures),
        kg_failure_sample=kg_failures[:2],
        privacy_actions=len(decision.privacy_actions),
        mentions=mentions_written if kg is not None else 0,
        mentions_rejected=mentions_rejected if kg is not None else 0,
    )
    await msg.ack()


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
) -> None:
    """Handle ``MemoryCommandPayload`` from ``eidolon.memory.cmd.<memory_space_token>``.

    Commands are **always acked** after processing; the wrapper's idempotency
    layer (LockedKnowledgeGraph G1) keeps redelivery safe, and admin actions
    are explicit user intent — DLQ semantics would lose intent. Failures are
    logged but don't NAK (the source is admin, not chat; chat ack-loop is the
    one that needs strong delivery guarantees).
    """
    del settings  # not currently consulted; reserved for future cmd kinds
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        kind = raw.get("kind")
        if kind == "kg_add_triple":
            cmd: MemoryCommandPayload = KgAddTripleCommand.model_validate(raw)
        elif kind == "kg_invalidate":
            cmd = KgInvalidateCommand.model_validate(raw)
        elif kind == "consolidator_ingest_theme":
            cmd = ConsolidatorIngestThemeCommand.model_validate(raw)
        elif kind == "user_confirm_fact":
            cmd = UserConfirmedFactCommand.model_validate(raw)
        elif kind == "device_sync_batch":
            cmd = DeviceSyncBatchPayload.model_validate(raw)
        else:
            log.error("cmd_unknown_kind", kind=kind)
            await msg.ack()
            return
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        log.error("cmd_bad_payload", error=str(exc))
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
        await msg.ack()
        return

    try:
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
        elif isinstance(cmd, KgInvalidateCommand):
            rows = await kg.invalidate(
                subject=cmd.subject,
                predicate=cmd.predicate,
                object=cmd.object,
                ended=cmd.ended,
            )
            log.info(
                "cmd_kg_invalidate_ok",
                request_id=cmd.request_id,
                rows=rows,
                subject=cmd.subject,
                predicate=cmd.predicate,
                object=cmd.object,
            )
        elif isinstance(cmd, ConsolidatorIngestThemeCommand):
            await _ingest_theme(backend, cmd)
            log.info(
                "cmd_theme_ingest_ok",
                request_id=cmd.request_id,
                underlying_wing=cmd.underlying_wing,
                drawer_count=len(cmd.source_drawer_ids),
                confidence=cmd.confidence,
            )
        elif isinstance(cmd, UserConfirmedFactCommand):
            await _ingest_user_confirmed(backend, cmd)
            log.info(
                "cmd_user_confirm_ok",
                request_id=cmd.request_id,
                wing=cmd.wing,
                memory_type=cmd.memory_type,
                confidence=cmd.confidence,
            )
        elif isinstance(cmd, DeviceSyncBatchPayload):
            log.info(
                "cmd_device_sync_batch_ignored_on_cmd_subject",
                request_id=cmd.request_id,
                events=len(cmd.events),
            )
    except Exception as exc:
        log.error("cmd_apply_failed", request_id=cmd.request_id, error=str(exc))

    await msg.ack()


async def process_sync_message(
    msg: Any,
    *,
    steward: StewardProtocol,
    backend: Any,
    ledger: Any,
    settings: MemorySettings,
    expected_memory_space_id: str,
) -> None:
    """Handle ``DeviceSyncBatchPayload`` from ``eidolon.memory.sync.<memory_space_token>``."""
    del settings
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        batch = DeviceSyncBatchPayload.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
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

            decision = await steward.decide(turn)
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


async def _ingest_theme(backend: Any, cmd: "ConsolidatorIngestThemeCommand") -> None:
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


async def _ingest_user_confirmed(
    backend: Any, cmd: "UserConfirmedFactCommand",
) -> None:
    """Write a user-confirmed fact directly as a drawer in the chosen wing.

    Bypasses the steward by design: a user-confirmed fact is the user's
    own ground truth — paraphrase / mis-classification / silent drop by
    the LLM are all unacceptable. Recall-time priority boost is keyed off
    ``metadata.source == "user-confirmed"``, so this string is the
    cross-layer contract; do not rename without updating recall too.

    Idempotency: ``fragment_id = "userconfirm:<request_id>"``; chroma's
    deterministic ids collapse redelivery to one row.
    """
    fragment = MemoryFragment(
        memory_id=f"userconfirm:{cmd.request_id}",
        memory_space_id=cmd.memory_space_id,
        memory_realm_id=cmd.memory_space_id,
        companion_id=cmd.source_instance_id or None,
        scope=cmd.scope,
        visibility=cmd.visibility,
        source_device_id=cmd.source_device_id or "admin",
        target_device_id=cmd.target_device_id,
        source_instance_id=cmd.source_instance_id or cmd.issuer,
        wing=cmd.wing,
        room=f"{USER_CONFIRMED_ROOM_PREFIX}{cmd.request_id[:16]}",
        content=cmd.text,
        memory_type=cmd.memory_type,
        importance=cmd.importance,
        confidence=cmd.confidence,
        occurred_at=cmd.issued_at,
        source_turn_id=f"user-confirmed:{cmd.request_id}",
        session_id=cmd.session_id or "user-confirmed",
        tags=["user-confirmed", *cmd.tags],
        privacy="normal",
        metadata={
            "source": "user-confirmed",
            "request_id": cmd.request_id,
        },
        extensions=cmd.extensions,
    )
    await ingest_memory_fragment(backend, fragment)
