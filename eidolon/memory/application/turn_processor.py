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

from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.kg import (
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryCommandPayload,
)
from eidolon.memory.domain.payloads import ConversationTurnPayload
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


async def _apply_privacy(backend: Any, user_id: str, actions: list) -> None:
    """Wrapper for the steward's privacy-action handler (delete / archive)."""
    if not actions:
        return
    from eidolon.memory.application.steward.common import apply_privacy_actions

    await apply_privacy_actions(backend, user_id=user_id, actions=actions)


async def process_turn_message(
    msg: Any,
    *,
    steward: StewardProtocol,
    backend: Any,
    kg: Any = None,
    settings: MemorySettings,
    max_deliveries: int,
    expected_user_id: str | None = None,
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

    if expected_user_id is not None and turn.user_id and turn.user_id != expected_user_id:
        log.error(
            "turn_processor_user_id_mismatch",
            expected=expected_user_id,
            got=turn.user_id,
            turn_id=turn.turn_id,
        )
        await msg.ack()
        return

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

    fallback_user = turn.user_id or "default"
    turn_ts = turn.timestamp  # used as default valid_from / ended for triples

    # ── fragments + privacy (failure here NAKs — chroma is source of truth) ─
    fragments_written = 0
    try:
        # Privacy actions first; they may purge before we attempt new writes.
        await _apply_privacy(backend, fallback_user, decision.privacy_actions)
        if decision.should_write:
            for fragment in decision.fragments:
                await ingest_memory_fragment(backend, fragment)
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

    # G8: one structured line per turn — operators can grep this without
    # parsing the whole log stream.
    log.info(
        "turn_processed",
        turn_id=turn.turn_id,
        user_id=turn.user_id,
        should_write=decision.should_write,
        fragments=fragments_written,
        triples=kg_triples_added,
        invalidations=kg_invalidations_applied,
        kg_skipped_lowconf=kg_skipped_low_confidence,
        kg_failures=len(kg_failures),
        kg_failure_sample=kg_failures[:2],
        privacy_actions=len(decision.privacy_actions),
    )
    await msg.ack()


async def process_command_message(
    msg: Any,
    *,
    backend: Any,
    kg: Any,
    settings: MemorySettings,
    expected_user_id: str | None = None,
) -> None:
    """Handle ``MemoryCommandPayload`` from ``agent.memory.cmd.<user_id>``.

    Commands are **always acked** after processing; the wrapper's idempotency
    layer (LockedKnowledgeGraph G1) keeps redelivery safe, and admin actions
    are explicit user intent — DLQ semantics would lose intent. Failures are
    logged but don't NAK (the source is admin, not chat; chat ack-loop is the
    one that needs strong delivery guarantees).
    """
    del backend, settings  # not used yet; reserved for future delete / privacy cmds
    try:
        raw = json.loads(msg.data.decode("utf-8"))
        kind = raw.get("kind")
        if kind == "kg_add_triple":
            cmd: MemoryCommandPayload = KgAddTripleCommand.model_validate(raw)
        elif kind == "kg_invalidate":
            cmd = KgInvalidateCommand.model_validate(raw)
        else:
            log.error("cmd_unknown_kind", kind=kind)
            await msg.ack()
            return
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        log.error("cmd_bad_payload", error=str(exc))
        await msg.ack()
        return

    if expected_user_id is not None and cmd.user_id and cmd.user_id != expected_user_id:
        log.error(
            "cmd_user_id_mismatch",
            expected=expected_user_id,
            got=cmd.user_id,
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
    except Exception as exc:
        log.error("cmd_apply_failed", request_id=cmd.request_id, error=str(exc))

    await msg.ack()
