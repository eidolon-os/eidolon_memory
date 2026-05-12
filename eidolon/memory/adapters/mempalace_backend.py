"""MemPalace access exclusively via MCP ``call_tool``."""

from __future__ import annotations

from typing import Any

from eidolon.memory.adapters.search_payload import parse_search_tool_payload
from eidolon.memory.config.ontology import OntologyConfig
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.mcp.runtime import MemPalaceMcpRuntime
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class McpMemPalaceBackend(MemoryBackend):
    """Maps logical memory operations to configured MemPalace MCP tool names."""

    def __init__(
        self,
        runtime: MemPalaceMcpRuntime,
        ontology: OntologyConfig,
        palace_path: str,
    ) -> None:
        self._rt = runtime
        self._ont = ontology
        self._palace = palace_path

    def _t(self) -> Any:
        return self._ont.mcp_tools

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        name = self._t().search_drawers
        args: dict[str, Any] = {
            "query": query,
            "wing": wing,
            "limit": n_results,
            "palace_path": self._palace,
        }
        if room:
            args["room"] = room
        data = await self._rt.call_tool(
            name,
            args,
            read_timeout_seconds=self._ont.recall.timeout_seconds,
        )
        hits = parse_search_tool_payload(data)
        return apply_recall_policy(hits, self._ont)

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        name = self._t().add_to_drawer
        args: dict[str, Any] = {
            "wing": wing,
            "room": room,
            "content": text,
            "palace_path": self._palace,
        }
        if metadata:
            args["metadata"] = metadata
        await self._rt.call_tool(name, args, read_timeout_seconds=120.0)

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        del user_id, key
        return None

    async def get_all(self, user_id: str) -> list[MemoryWireRecord]:
        del user_id
        return []

    async def delete(self, user_id: str, key: str) -> None:
        tool = (self._t().delete_document or "").strip()
        if not tool:
            log.debug("mcp_delete_skipped_no_tool", wing=user_id, room=key)
            return
        await self._rt.call_tool(
            tool,
            {"wing": user_id, "room": key, "palace_path": self._palace},
            read_timeout_seconds=30.0,
        )


def apply_recall_policy(
    hits: list[MemoryWireRecord],
    ont: OntologyConfig,
) -> list[MemoryWireRecord]:
    """Filter taboo/archived and cap top_k (post-parse safety net)."""
    blocked = {s.lower() for s in ont.recall.filter_taboo_statuses}
    filtered: list[MemoryWireRecord] = []
    for h in hits:
        st = str(h.metadata.get("room_status", "")).lower()
        if st and st in blocked:
            continue
        filtered.append(h)
    return filtered[: ont.recall.top_k]
