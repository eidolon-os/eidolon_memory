"""LiveKit voice hot-path recall (D1): in-process, hard timeout, fail-fast.

In D1 the agent_runner process owns a single ``LockedBackend`` shared by both
read and write paths; this service is a thin wrapper that adds the hard timeout
and degraded-result envelope expected by the LiveKit pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.application.public_recall import (
    recall_with_kg_fusion,
    wire_record_to_public_dict,
)
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class LiveKitRecallService:
    """Recall long-term memory before TTS pipeline continues. Never raises."""

    def __init__(
        self,
        backend: MemoryBackend,
        settings: MemorySettings,
        *,
        palace_path: str,
        kg: Any = None,
    ) -> None:
        self._backend = backend
        self._settings = settings
        self._palace_path = palace_path
        self._kg = kg

    async def recall_context(
        self,
        query: str,
        *,
        context: MemoryActorContext,
        top_k: int | None = None,
    ) -> str:
        """Return grouped context text; fail-fast → empty string on timeout/error."""
        result = await self.recall_context_with_records(
            query,
            context=context,
            top_k=top_k,
        )
        return result["context"]

    async def recall_context_with_records(
        self,
        query: str,
        *,
        context: MemoryActorContext,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        k = top_k if top_k is not None else self._settings.recall.top_k
        timeout = self._settings.recall.livekit_timeout_seconds
        try:
            fused = await asyncio.wait_for(
                recall_with_kg_fusion(
                    self._backend,
                    self._settings,
                    query=query,
                    context=context,
                    top_k=k,
                    kg=self._kg,
                    for_voice=True,
                    palace_path=self._palace_path,
                ),
                timeout=timeout,
            )
            vector_records = fused["vector"]
            kg_records = fused["kg"]
            return {
                "context": group_recall_context(vector_records, kg_triples=kg_records),
                "records": [wire_record_to_public_dict(r) for r in vector_records],
                "kg_triples": [t.model_dump(mode="json") for t in kg_records],
                "degraded": bool(fused.get("degraded", False)),
                "degraded_reason": fused.get("degraded_reason"),
                "trace": dict(fused.get("trace") or {}),
            }
        except TimeoutError:
            log.warning("livekit_recall_degraded", reason="timeout", query_len=len(query))
            return {
                "context": "",
                "records": [],
                "kg_triples": [],
                "degraded": True,
                "degraded_reason": "timeout",
                "trace": {},
            }
        except Exception as exc:
            log.warning("livekit_recall_degraded", reason="error", error=str(exc))
            return {
                "context": "",
                "records": [],
                "kg_triples": [],
                "degraded": True,
                "degraded_reason": str(exc),
                "trace": {},
            }
