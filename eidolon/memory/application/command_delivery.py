"""Truthful delivery status for asynchronous Memory commands."""

from __future__ import annotations

from typing import Any

from eidolon.memory.domain.ports import CommandStatusStore
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

MAX_WAIT_SECONDS = 10.0


async def publish_with_status(
    command_publisher: Any,
    command_status: CommandStatusStore | None,
    command: Any,
    *,
    wait_seconds: float,
) -> dict[str, Any]:
    """Publish once and report only status proven by the command ledger."""

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


__all__ = ["MAX_WAIT_SECONDS", "publish_with_status"]
