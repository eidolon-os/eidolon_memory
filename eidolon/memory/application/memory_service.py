"""Legacy NATS RPC MemoryService — writes and CRUD only (no semantic read RPC).

Semantic **search / recall** is performed through the owned MCP read server
or ``McpRecallClient`` over the Python MemPalace backend; this service does
not subscribe to ``MEMORY_QUERY``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from eidolon.memory.infrastructure.bus import BusEnvelope, BusHeader, SharedSubjects
from eidolon.memory.application.ingest import ingest_fragment
from eidolon.memory.domain.payloads import (
    MemoryDeletePayload,
    MemoryGetAllPayload,
    MemoryGetPayload,
    MemoryResultPayload,
    MemoryStorePayload,
)
from eidolon.memory.domain.errors import MemoryBackendUnsupported
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.support.logging import get_logger

if TYPE_CHECKING:
    from eidolon.memory.domain.ports import MemoryBackend

log = get_logger(__name__)


def _wire_to_agent_dict(rec: MemoryWireRecord) -> dict[str, Any]:
    """Wire record dict shape matches ``MemoryRecord`` for NATS clients."""
    return rec.model_dump(mode="json")


class MemoryService:
    """NATS RPC service: ``MEMORY_STORE`` / get / delete → ``MemoryBackend`` (MCP).

    Semantic search is **not** handled over NATS; use MCP recall in the agent process.

    Instantiate with an already-started ``MemoryBackend`` (typically
        :class:`eidolon.memory.adapters.MemPalacePythonBackend`).
    """

    def __init__(
        self,
        bus_broker: Any,
        backend: MemoryBackend,
    ) -> None:
        self._broker = bus_broker
        self._backend = backend
        self._ingest_lock = asyncio.Lock()
        self._running = False

    def _publish_result(
        self,
        reply_to: str | None,
        correlation_id: str,
        records: list[MemoryWireRecord],
        error: str | None = None,
    ) -> None:
        if not reply_to:
            return
        payload = MemoryResultPayload(
            correlation_id=correlation_id,
            results=[_wire_to_agent_dict(r) for r in records],
            error=error,
        )
        asyncio.create_task(
            self._broker.publish(
                reply_to,
                BusEnvelope(
                    header=BusHeader(source="memory-service"),
                    payload=payload.model_dump(),
                ).model_dump(),
            )
        )

    async def _on_store(self, body: dict, reply_to: str | None = None) -> None:
        try:
            envelope = BusEnvelope.model_validate(body)
            payload = MemoryStorePayload(**envelope.payload)
        except Exception as exc:
            log.error("memory_store_parse_error", error=str(exc))
            return

        try:
            user_id = payload.wing or payload.user_id or "default"
            key = payload.room or "general"
            metadata: dict[str, Any] = {"source_file": payload.source_file or ""}
            if payload.occurred_at:
                metadata["occurred_at"] = payload.occurred_at
            if payload.verbatim is not None:
                metadata["verbatim"] = payload.verbatim
            await ingest_fragment(
                self._backend,
                wing=user_id,
                room=key,
                text=payload.text,
                metadata=metadata,
                serialize_lock=self._ingest_lock,
            )
            error: str | None = None
        except Exception as exc:
            log.error("memory_ingest_error", error=str(exc))
            error = str(exc)

        self._publish_result(reply_to, payload.correlation_id, [], error)

    async def _on_get(self, body: dict, reply_to: str | None = None) -> None:
        try:
            envelope = BusEnvelope.model_validate(body)
            payload = MemoryGetPayload(**envelope.payload)
        except Exception as exc:
            log.error("memory_get_parse_error", error=str(exc))
            return

        try:
            record = await self._backend.get(payload.user_id, payload.key)
            records = [record] if record else []
            error: str | None = None
        except MemoryBackendUnsupported as exc:
            log.warning("memory_get_unsupported", error=str(exc))
            records = []
            error = str(exc)
        except Exception as exc:
            log.error("memory_get_error", error=str(exc))
            records = []
            error = str(exc)

        self._publish_result(reply_to, payload.correlation_id, records, error)

    async def _on_get_all(self, body: dict, reply_to: str | None = None) -> None:
        try:
            envelope = BusEnvelope.model_validate(body)
            payload = MemoryGetAllPayload(**envelope.payload)
        except Exception as exc:
            log.error("memory_get_all_parse_error", error=str(exc))
            return

        try:
            records = await self._backend.get_all(payload.user_id)
            error: str | None = None
        except MemoryBackendUnsupported as exc:
            log.warning("memory_get_all_unsupported", error=str(exc))
            records = []
            error = str(exc)
        except Exception as exc:
            log.error("memory_get_all_error", error=str(exc))
            records = []
            error = str(exc)

        self._publish_result(reply_to, payload.correlation_id, records, error)

    async def _on_delete(self, body: dict, reply_to: str | None = None) -> None:
        try:
            envelope = BusEnvelope.model_validate(body)
            payload = MemoryDeletePayload(**envelope.payload)
        except Exception as exc:
            log.error("memory_delete_parse_error", error=str(exc))
            return

        try:
            await self._backend.delete(payload.user_id, payload.key)
            error: str | None = None
        except MemoryBackendUnsupported as exc:
            log.warning("memory_delete_unsupported", error=str(exc))
            error = str(exc)
        except Exception as exc:
            log.error("memory_delete_error", error=str(exc))
            error = str(exc)

        self._publish_result(reply_to, payload.correlation_id, [], error)

    async def start(self) -> None:
        if self._running:
            return
        self._broker.subscriber(SharedSubjects.MEMORY_STORE)(self._on_store)
        self._broker.subscriber(SharedSubjects.MEMORY_GET)(self._on_get)
        self._broker.subscriber(SharedSubjects.MEMORY_GET_ALL)(self._on_get_all)
        self._broker.subscriber(SharedSubjects.MEMORY_DELETE)(self._on_delete)
        self._running = True
        log.info("memory_service_started")
