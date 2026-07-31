"""Adapter from eidolon_data MemoryEnginePort to eidolon_memory backends."""

from __future__ import annotations

from typing import Any

from eidolon_data.ports import MemoryEnginePort
from eidolon_data.schema.types import (
    MemoryEngineHealth,
    MemoryIngestResult,
    MemoryItem,
    RecallHit,
    RecallOptions,
    RecallResult,
)

from eidolon.memory.domain.ports import MemoryBackend


class EidolonDataMemoryEngine(MemoryEnginePort):
    """Expose an eidolon_memory backend through eidolon_data's memory port.

    `realm_id` maps to the MemPalace wing. Optional `room` can be supplied in
    item metadata/payload or recall filters. This keeps Eidolon Data aware of
    sovereign memory ownership while leaving vector/KG/backend internals inside
    eidolon_memory/MemPalace.
    """

    def __init__(self, backend: MemoryBackend, *, engine_name: str = "mempalace") -> None:
        self._backend = backend
        self._engine_name = engine_name

    async def ingest(self, item: MemoryItem) -> MemoryIngestResult:
        room = _item_room(item)
        text = _item_text(item)
        metadata = {
            **item.metadata,
            "memory_id": item.memory_id,
            "realm_id": item.realm_id,
            "item_type": item.item_type,
            "privacy": item.privacy,
            "importance": item.importance,
            "confidence": item.confidence,
            "payload": item.payload,
        }
        await self._backend.ingest_text(
            wing=item.realm_id,
            room=room,
            text=text,
            metadata=metadata,
        )
        return MemoryIngestResult(
            memory_id=item.memory_id,
            realm_id=item.realm_id,
            engine=self._engine_name,
            external_key=f"{item.realm_id}/{room}/{item.memory_id}",
            metadata={"wing": item.realm_id, "room": room},
        )

    async def recall(self, realm_id: str, query: str, options: RecallOptions) -> RecallResult:
        room = options.filters.get("room") if options.filters else None
        records = await self._backend.search(
            query,
            wing=realm_id,
            room=str(room) if room else None,
            n_results=options.top_k,
        )
        hits = [
            RecallHit(
                memory_id=str(record.metadata.get("memory_id") or record.key),
                score=_score(record.metadata),
                content=str(record.value or ""),
                metadata=record.metadata,
            )
            for record in records
        ]
        return RecallResult(
            hits=hits,
            metadata={"engine": self._engine_name, "realm_id": realm_id},
        )

    async def delete(self, memory_id: str) -> None:
        # The lower-level admin surface needs both a wing/user id and key.
        # Keeping this no-op avoids inventing an unsafe mapping in the data layer.
        del memory_id

    async def health(self) -> MemoryEngineHealth:
        return MemoryEngineHealth(ok=True, engine=self._engine_name)


def _item_room(item: MemoryItem) -> str:
    raw = item.metadata.get("room") or item.payload.get("room") or item.item_type or "default"
    return str(raw)


def _item_text(item: MemoryItem) -> str:
    for value in (
        item.payload.get("content"),
        item.payload.get("text"),
        item.payload.get("value"),
        item.content_summary,
    ):
        if value:
            return str(value)
    return item.memory_id


def _score(metadata: dict[str, Any]) -> float:
    for key in ("score", "similarity", "confidence"):
        value = metadata.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0
