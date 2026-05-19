"""LiveKit voice hot-path recall (D1): in-process, hard timeout, fail-fast.

In D1 the agent_runner process owns a single ``LockedBackend`` shared by both
read and write paths; this service is a thin wrapper that adds the hard timeout
and degraded-result envelope expected by the LiveKit pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from eidolon.memory.application.public_recall import (
    group_recall_context,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord
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
    ) -> None:
        self._backend = backend
        self._settings = settings
        self._palace_path = palace_path

    async def recall_context(
        self,
        query: str,
        *,
        user_id: str = "default",
        session_id: str = "",
        top_k: int | None = None,
    ) -> str:
        """Return grouped context text; fail-fast → empty string on timeout/error."""
        result = await self.recall_context_with_records(
            query,
            user_id=user_id,
            session_id=session_id,
            top_k=top_k,
        )
        return result["context"]

    async def recall_context_with_records(
        self,
        query: str,
        *,
        user_id: str = "default",
        session_id: str = "",
        top_k: int | None = None,
    ) -> dict[str, Any]:
        k = top_k if top_k is not None else self._settings.recall.top_k
        timeout = self._settings.recall.livekit_timeout_seconds
        try:
            records = await asyncio.wait_for(
                self._recall_records(
                    query,
                    user_id=user_id,
                    session_id=session_id,
                    top_k=k,
                ),
                timeout=timeout,
            )
            return {
                "context": group_recall_context(records),
                "records": [wire_record_to_public_dict(r) for r in records],
                "degraded": False,
            }
        except TimeoutError:
            log.warning("livekit_recall_degraded", reason="timeout", query_len=len(query))
            return {"context": "", "records": [], "degraded": True}
        except Exception as exc:
            log.warning("livekit_recall_degraded", reason="error", error=str(exc))
            return {"context": "", "records": [], "degraded": True}

    async def _recall_records(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        top_k: int,
    ) -> list[MemoryWireRecord]:
        return await search_all_wings_mcp_style(
            self._backend,
            self._settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            wing=None,
            room=None,
            for_voice=True,
            session_id=session_id,
            user_utterance=query,
            palace_path=self._palace_path,
        )
