"""LiveKit voice hot-path recall: in-process, hard timeout, fail-fast."""

from __future__ import annotations

import asyncio
from typing import Any

from eidolon.memory.application.public_recall import (
    group_recall_context,
    search_all_wings_mcp_style,
    wire_record_to_public_dict,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.chroma_refresh import (
    is_database_locked_error,
    is_disk_io_error,
    is_transient_index_error,
)
from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class LiveKitRecallService:
    """Recall long-term memory for LLM prompt injection before TTS pipeline continues."""

    def __init__(self, session: PalaceReadSession, settings: MemorySettings) -> None:
        from eidolon.memory.infrastructure.cpu_env import apply_cpu_thread_env

        apply_cpu_thread_env(settings, role="livekit")
        self._session = session
        self._settings = settings

    async def recall_context(
        self,
        query: str,
        *,
        user_id: str = "default",
        session_id: str = "",
        top_k: int | None = None,
    ) -> str:
        """Return grouped context text; never raises (fail-fast → empty string)."""
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
            await self._session.background_reconcile()
            return {"context": "", "records": [], "degraded": True}
        except Exception as exc:
            reason = "error"
            if is_database_locked_error(exc) or is_disk_io_error(exc):
                reason = "database_locked"
            elif is_transient_index_error(exc):
                reason = "transient_index"
            log.warning(
                "livekit_recall_degraded",
                reason=reason,
                error=str(exc),
            )
            await self._session.background_reconcile()
            return {"context": "", "records": [], "degraded": True}

    async def _recall_records(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        top_k: int,
    ) -> list[MemoryWireRecord]:
        await self._session.ensure_fresh()
        backend = await self._session.active_backend()
        return await search_all_wings_mcp_style(
            backend,
            self._settings,
            query=query,
            user_id=user_id,
            top_k=top_k,
            wing=None,
            room=None,
            for_voice=True,
            session_id=session_id,
            user_utterance=query,
            palace_path=self._session.palace_path,
        )
