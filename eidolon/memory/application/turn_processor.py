"""Process JetStream messages: ConversationTurn + MemoryCommand (KG plan §4.4).

Used by ``agent_runner``'s in-process NATS subscriber. Steward runs in-process;
backend writes go through the same ``LockedBackend`` as reads.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.kg import (
    KgAddTripleCommand,
    KgInvalidateCommand,
    MemoryCommandPayload,
)
from eidolon.memory.domain.payloads import ConversationTurnPayload
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class StewardProtocol(Protocol):
    async def handle_turn(self, turn: ConversationTurnPayload, backend: Any) -> None:
        ...


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
    """Decode + validate one JetStream message, run steward, ack / nak / DLQ.

    ``kg`` is the per-runner :class:`LockedKnowledgeGraph`; in T1 it's accepted
    but not yet written to here (Steward returns triples in T2). The signature
    is forward-compatible so we don't churn callers later.

    No generation bumping (D1: same process owns reads, no cross-process notify needed).
    ``expected_user_id`` lets the caller reject messages whose payload user_id
    doesn't match the agent runner's bound user (defense-in-depth on top of subject filter).
    """
    del kg  # T1 placeholder — wired but unused; T2 consumes decision.triples
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

    try:
        await steward.handle_turn(turn, backend)
        await msg.ack()
        log.debug("turn_processor_acked", turn_id=turn.turn_id)
    except Exception as exc:
        log.error(
            "turn_processor_failed",
            error=str(exc),
            deliveries=deliveries,
            turn_id=getattr(turn, "turn_id", ""),
        )
        if deliveries >= max_deliveries:
            append_dlq(settings, msg.data, str(exc), deliveries)
            await msg.ack()
            log.error("turn_processor_dlq_ack", deliveries=deliveries)
        else:
            await msg.nak()


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
        # Discriminate on `kind` field
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
            expected=expected_user_id, got=cmd.user_id, request_id=cmd.request_id,
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
                request_id=cmd.request_id, triple_id=triple_id,
                subject=cmd.subject, predicate=cmd.predicate, object=cmd.object,
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
                request_id=cmd.request_id, rows=rows,
                subject=cmd.subject, predicate=cmd.predicate, object=cmd.object,
            )
    except Exception as exc:
        log.error("cmd_apply_failed", request_id=cmd.request_id, error=str(exc))

    await msg.ack()
