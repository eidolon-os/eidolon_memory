"""MemPalace integration through its in-process Python API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eidolon.memory.adapters.search_payload import parse_search_tool_payload
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import (
    MemoryBackendUnavailable,
    MemoryBackendUnsupported,
    MemoryBackendWriteFailed,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.chroma_refresh import ensure_sqlite_wal
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class MemPalacePythonBackend(MemoryBackend):
    """Maps Eidolon memory operations to MemPalace's Python package.

    D1: applies ``synchronous=FULL`` (configurable via ``settings.chromadb``) on
    construction so chroma's SQLite commits fsync — required for hard-kill
    durability since writes are async (NATS-driven) and a 30% commit overhead is
    cheap in this workload.
    """

    def __init__(self, settings: MemorySettings, palace_path: str) -> None:
        self._settings = settings
        self._palace = palace_path
        self._apply_chromadb_pragmas()

    def _apply_chromadb_pragmas(self) -> None:
        sqlite_path = Path(self._palace) / "chroma.sqlite3"
        if not sqlite_path.is_file():
            return  # palace not yet initialized; agent_runner lazy-init will handle it
        sync = (self._settings.chromadb.synchronous or "FULL").upper()
        try:
            info = ensure_sqlite_wal(str(sqlite_path), synchronous=sync)
            log.info("mempalace_chroma_pragmas_applied", palace=self._palace, **info)
        except Exception as exc:  # PRAGMA failures are non-fatal
            log.warning("mempalace_chroma_pragmas_failed", palace=self._palace, error=str(exc))

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        return await asyncio.to_thread(
            self.search_sync,
            query,
            wing=wing,
            n_results=n_results,
            room=room,
        )

    def search_sync(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        try:
            from mempalace.searcher import search_memories
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc

        data = search_memories(
            query=query,
            palace_path=self._palace,
            wing=wing,
            room=room,
            n_results=n_results,
        )
        if isinstance(data, dict) and data.get("error"):
            raise MemoryBackendUnavailable(str(data.get("error")))
        return apply_recall_policy(parse_search_tool_payload(data), self._settings)

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            collection = _get_collection(self._palace, create=True)
            wing = _sanitize_name(wing, "wing")
            room = _sanitize_name(room, "room")
            content = _sanitize_content(text)
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except ValueError as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc

        drawer_id = _drawer_id(wing, room, content)
        meta = _metadata_for_chroma(
            {
                **(metadata or {}),
                "wing": wing,
                "room": room,
                "source_file": (metadata or {}).get("source_file", ""),
                "chunk_index": 0,
                "added_by": (metadata or {}).get("added_by", "eidolon-memory"),
                "filed_at": datetime.now().isoformat(),
            }
        )
        try:
            existing = collection.get(ids=[drawer_id], include=[])
            if _ids(existing):
                return
            collection.upsert(ids=[drawer_id], documents=[content], metadatas=[meta])
            inserted = collection.get(ids=[drawer_id], include=[])
            if not _ids(inserted):
                msg = "MemPalace acknowledged write but drawer is not readable"
                raise MemoryBackendWriteFailed(msg)
        except MemoryBackendWriteFailed:
            raise
        except Exception as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        metadata = {
            **fragment.metadata,
            "fragment_id": fragment.fragment_id,
            "user_id": fragment.user_id,
            "source_turn_id": fragment.source_turn_id,
            "schema_version": "1",
            "session_id": fragment.session_id,
            "importance": fragment.importance,
            "confidence": fragment.confidence,
            "memory_type": fragment.memory_type,
            "privacy": fragment.privacy,
            "tags": fragment.tags,
        }
        if fragment.occurred_at:
            metadata["occurred_at"] = fragment.occurred_at
        await self.ingest_text(
            wing=fragment.wing,
            room=fragment.room,
            text=fragment.content,
            metadata=metadata,
        )

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        del user_id
        try:
            collection = _get_collection(self._palace, create=False)
            result = collection.get(ids=[key], include=["documents", "metadatas"])
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except Exception as exc:
            raise MemoryBackendUnavailable(str(exc)) from exc
        if not _ids(result):
            return None
        return _record_from_get_result(result, 0, drawer_id=key)

    async def get_all(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        """List drawers filtered by tenant, or enumerate the palace when tenant is blank.

        Non-blank tenant: Steward puts companion id in metadata ``user_id`` and semantic
        wing in ``wing``. Both are queried via ``$or`` to cover legacy rows that only
        set ``metadata.wing`` to the tenant slug.

        Blank ``user_id``: return a page of all drawers in the collection (no ``where``
        clause; order is backend-defined).
        """
        try:
            collection = _get_collection(self._palace, create=False)
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc

        tenant = user_id.strip()
        if not tenant:
            try:
                result = collection.get(
                    include=["documents", "metadatas"],
                    limit=limit,
                    offset=offset or 0,
                )
                return [
                    _record_from_get_result(result, idx, drawer_id=drawer_id)
                    for idx, drawer_id in enumerate(_ids(result))
                ]
            except Exception as exc:
                raise MemoryBackendUnavailable(str(exc)) from exc

        tenant_where: dict[str, Any] = {
            "$or": [
                {"user_id": tenant},
                {"wing": tenant},
            ]
        }

        try:
            result = collection.get(
                where=tenant_where,
                include=["documents", "metadatas"],
                limit=limit,
                offset=offset or 0,
            )
            return [
                _record_from_get_result(result, idx, drawer_id=drawer_id)
                for idx, drawer_id in enumerate(_ids(result))
            ]
        except Exception:
            merged = _merge_tenant_queries(collection, tenant_id=tenant)
            sliced = merged[offset or 0 :]
            if limit is not None:
                sliced = sliced[:limit]
            return sliced

    async def delete(self, user_id: str, key: str) -> None:
        del user_id
        if not key.startswith("drawer_"):
            msg = "delete expects a MemPalace drawer_id key"
            raise MemoryBackendUnsupported(msg)
        try:
            collection = _get_collection(self._palace, create=False)
            collection.delete(ids=[key])
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except Exception as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc


def _merge_tenant_queries(collection: Any, *, tenant_id: str) -> list[MemoryWireRecord]:
    """Combine ``user_id`` and ``wing`` filters when compound ``where`` is unsupported."""
    by_id: dict[str, MemoryWireRecord] = {}

    try:
        u = collection.get(
            where={"user_id": tenant_id},
            include=["documents", "metadatas"],
        )
        for idx, drawer_id in enumerate(_ids(u)):
            by_id[drawer_id] = _record_from_get_result(u, idx, drawer_id=drawer_id)
    except Exception:
        pass

    try:
        w = collection.get(
            where={"wing": tenant_id},
            include=["documents", "metadatas"],
        )
        for idx, drawer_id in enumerate(_ids(w)):
            if drawer_id not in by_id:
                by_id[drawer_id] = _record_from_get_result(w, idx, drawer_id=drawer_id)
    except Exception:
        pass

    return list(by_id.values())


def apply_recall_policy(
    hits: list[MemoryWireRecord],
    settings: MemorySettings,
) -> list[MemoryWireRecord]:
    """Filter private/archived memories and cap top_k."""
    blocked = {s.lower() for s in settings.recall.filter_taboo_statuses}
    filtered: list[MemoryWireRecord] = []
    for hit in hits:
        if hit.metadata.get("wing") == "Wing_Privacy" or hit.user_id == "Wing_Privacy":
            continue
        privacy = str(hit.metadata.get("privacy", "")).lower()
        if privacy in {"private", "do_not_recall"}:
            continue
        status = str(hit.metadata.get("room_status", "")).lower()
        if status and status in blocked:
            continue
        filtered.append(hit)
    return filtered[: settings.recall.top_k]


def _get_collection(palace_path: str, *, create: bool):
    from mempalace.palace import get_collection

    return get_collection(palace_path, create=create)


def _sanitize_name(value: str, field_name: str) -> str:
    try:
        from mempalace.config import sanitize_name

        return sanitize_name(value, field_name)
    except ImportError:
        raise
    except Exception:
        value = value.strip()
        if not value:
            raise ValueError(f"{field_name} cannot be blank")
        return value


def _sanitize_content(value: str) -> str:
    try:
        from mempalace.config import sanitize_content

        return sanitize_content(value)
    except ImportError:
        raise
    except Exception:
        value = value.strip()
        if not value:
            raise ValueError("content cannot be blank")
        return value


def _drawer_id(wing: str, room: str, content: str) -> str:
    digest = hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{digest}"


def _metadata_for_chroma(metadata: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, str | int | float | bool):
            out[key] = value
        else:
            out[key] = json.dumps(value, ensure_ascii=False)
    return out or {"source": "eidolon-memory"}


def _ids(result: Any) -> list[str]:
    if isinstance(result, dict):
        return list(result.get("ids") or [])
    return list(getattr(result, "ids", []) or [])


def _documents(result: Any) -> list[str]:
    if isinstance(result, dict):
        return list(result.get("documents") or [])
    return list(getattr(result, "documents", []) or [])


def _metadatas(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        return list(result.get("metadatas") or [])
    return list(getattr(result, "metadatas", []) or [])


def _record_from_get_result(result: Any, index: int, *, drawer_id: str) -> MemoryWireRecord:
    docs = _documents(result)
    metas = _metadatas(result)
    meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
    content = docs[index] if index < len(docs) else ""
    wing = str(meta.get("wing", meta.get("user_id", "default")))
    room = str(meta.get("room", drawer_id))
    return MemoryWireRecord(
        user_id=wing,
        key=drawer_id or room,
        value=content,
        metadata={**meta, "wing": wing, "room": room, "source": "mempalace-python"},
    )
