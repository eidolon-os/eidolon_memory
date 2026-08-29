"""Writes a caller must be told the truth about, and the wait that makes that possible.

A conversation turn is published and forgotten — the caller is off the hook once
the bus has it. An **explicit** write is different: when someone says "记住我对
花生过敏", the assistant is about to say it has been remembered, so the caller
needs a real answer before it speaks.

That is what ``publish_with_status`` is for, and why it is here rather than
inside a tool. Publishing is durable on its own; the wait is over the lightweight
command-status projection, never over storage. A timeout downgrades to
``accepted``, and **never** to ``applied`` — a caller that says "I'll remember
that" on ``accepted`` is lying, and this is the one place that distinction is
enforced for every explicit write regardless of which surface asked for it.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from eidolon_memory_contracts import (
    MemoryActorContext,
    MemoryIntent,
    MemoryIntentCommand,
)

from eidolon.memory.application.claim_routing import route_explicit_claim
from eidolon.memory.domain.ports import CommandStatusStore
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: Ceiling on how long an explicit write may block its caller.
#:
#: The caller is holding a reply open. Ten seconds is already far past the point
#: where waiting beats answering honestly that the write is in flight.
MAX_WAIT_SECONDS = 10.0

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]+")


class InvalidWriteRequest(ValueError):
    """A caller-supplied value this layer will not accept."""


def normalise_request_id(raw: str | None) -> str:
    """A caller's idempotency key, or a fresh one.

    Restricted because it ends up in a NATS subject-adjacent identifier and in
    ledger keys. Rejected rather than sanitised: silently altering the key a
    caller retries under would make the retry a second write.
    """

    clean = (raw or "").strip()
    if not clean:
        return uuid.uuid4().hex
    if len(clean) > 128 or _REQUEST_ID_RE.fullmatch(clean) is None:
        raise InvalidWriteRequest("request_id contains unsupported characters")
    return clean


async def publish_with_status(
    command_publisher: Any,
    command_status: CommandStatusStore | None,
    command: Any,
    *,
    wait_seconds: float,
) -> dict[str, Any]:
    """Durably publish a write, then wait only on the lightweight projection.

    Returns the status dictionary the ledger records, or a truthful fallback:

    * publish raised          → ``failed``, and the failure is recorded so a
      later status lookup agrees with what the caller was told
    * no ledger configured    → ``accepted``; the write is durable on the bus but
      its outcome is unobservable, and claiming otherwise would be a guess
    * ledger unreadable       → ``accepted``, for the same reason
    * wait elapsed            → ``accepted``

    Never ``applied`` unless the ledger says so.
    """

    try:
        await command_publisher.publish(command)
    except Exception as exc:  # noqa: BLE001 - surface a truthful outcome
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
            timeout_seconds=max(0.0, min(wait_seconds, MAX_WAIT_SECONDS)),
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


def build_confirmed_fact_command(
    ctx: MemoryActorContext,
    text: str,
    *,
    request_id: str,
    source_event_id: str = "",
    tool_call_id: str = "",
    confidence: float = 0.99,
    tags: tuple[str, ...] = (),
    now_iso: str,
    wing: str = "auto",
    memory_type: str = "auto",
    importance: int = 5,
    scope: str = "persona",
    visibility: str = "all_devices",
    source_device_id: str = "",
    target_device_id: str | None = None,
    source_instance_id: str = "",
    council_id: str = "",
    session_id: str = "",
    extensions: dict[str, dict[str, Any]] | None = None,
) -> tuple[MemoryIntentCommand, dict[str, Any]]:
    """Turn "remember this, exactly" into a command, and say where it will land.

    One builder for both surfaces. The contract method takes only what a product
    caller knows — the sentence, and which turn asked for it — and lets routing
    choose the wing; the operator tool exposes the rest as overrides. Sharing the
    construction is what keeps them the same write rather than two writes that
    resemble each other.

    The second return value is the routing decision, which the operator tool
    reports back so a human can see where their fact went.
    """

    clean = (text or "").strip()
    if not clean:
        raise InvalidWriteRequest("text must be a non-empty string")

    requested_type = (memory_type or "auto").strip() or "auto"
    intent_type = "preference" if requested_type.lower() == "preference" else "fact"
    route = route_explicit_claim(clean, intent_type=intent_type)
    selected_wing = route.wing if (wing or "").strip() in {"", "auto"} else wing.strip()
    selected_type = (
        route.memory_type if requested_type.lower() == "auto" else requested_type
    )
    event_id = (source_event_id or "").strip() or request_id

    intent = MemoryIntent(
        intent_id=f"intent:{request_id}",
        memory_space_id=ctx.memory_realm_id,
        source_event_id=event_id,
        authority="explicit_user",
        intent_type=intent_type,
        raw_claim=clean,
        operation_hint="confirm",
        occurred_at=now_iso,
        tool_call_id=(tool_call_id or "").strip() or None,
        confidence=max(0.0, min(1.0, confidence)),
        attributes={
            "wing": selected_wing,
            "memory_type": selected_type,
            "importance": max(1, min(5, importance)),
            "tags": list(tags or []),
            "scope": scope,
            "visibility": visibility,
            "source_device_id": source_device_id,
            "target_device_id": target_device_id,
            "source_instance_id": source_instance_id or ctx.companion_id or "",
            "council_id": council_id or ctx.council_id or "",
            "session_id": session_id,
            "extensions": dict(extensions or {}),
        },
    )
    command = MemoryIntentCommand(
        request_id=request_id,
        memory_space_id=ctx.memory_realm_id,
        issued_at=now_iso,
        issuer="agent",
        intent=intent,
    )
    return command, {
        "wing": selected_wing,
        "memory_type": selected_type,
        "intent_id": intent.intent_id,
        "source_event_id": event_id,
    }


__all__ = [
    "MAX_WAIT_SECONDS",
    "InvalidWriteRequest",
    "build_confirmed_fact_command",
    "normalise_request_id",
    "publish_with_status",
]
