"""MemPalace integration through its in-process Python API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from eidolon.memory.adapters.mempalace_fast_search import search_memories_shared_embedding
from eidolon.memory.adapters.search_payload import parse_search_tool_payload
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import (
    MemoryBackendUnavailable,
    MemoryBackendUnsupported,
    MemoryBackendWriteFailed,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord, parse_memory_datetime
from eidolon.memory.infrastructure.mempalace_backend import selected_mempalace_backend
from eidolon.memory.infrastructure.mempalace_hnsw import probe_hnsw_safety
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemPalacePythonBackend(MemoryBackend):
    """Maps Eidolon memory operations to MemPalace's Python package.

    Chroma exclusively owns the SQLite journal and compaction lifecycle. Eidolon
    must not mutate or checkpoint ``chroma.sqlite3`` through a second SQLite
    connection while the native client is active.

    ``lock`` is None on this raw adapter — it's meant to be wrapped by
    ``LockedBackend`` (which adds the per-palace asyncio.Lock). Direct use
    of this class without the wrapper bypasses D1's single-owner serialization.
    """

    lock: asyncio.Lock | None = None
    working_memory: Any = None  # Phase 2 ring; agent_runner bolts it on at start
    supports_scoped_search = True

    def __init__(
        self,
        settings: MemorySettings,
        palace_path: str,
        *,
        memory_space_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._palace = palace_path
        # A palace hosts exactly one memory space. MemPalace's vector search
        # drops custom metadata, so search hits come back without a
        # ``memory_space_id`` — this authoritative id is stamped onto them so
        # recall's visibility gate (which compares against the caller's space)
        # doesn't reject every vector hit. See parse_search_tool_payload.
        self._memory_space_id = memory_space_id

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
        if selected_mempalace_backend(self._settings) == "sqlite_exact":
            try:
                collection = _get_collection(self._palace, create=False)
                where: dict[str, Any] = {"wing": wing}
                if room:
                    where = {"$and": [where, {"room": room}]}
                result = collection.query(
                    query_embeddings=[_deterministic_embedding(query)],
                    n_results=n_results,
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
                return apply_recall_policy(_records_from_query_result(result), self._settings)
            except ImportError as exc:
                raise MemoryBackendUnavailable("mempalace package is not installed") from exc
            except Exception as exc:
                raise MemoryBackendUnavailable(str(exc)) from exc

        try:
            from mempalace.searcher import search_memories
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc

        hnsw_safety = probe_hnsw_safety(self._palace)
        if hnsw_safety.vector_disabled:
            log.warning(
                "mempalace_hnsw_vector_disabled",
                palace=self._palace,
                status=hnsw_safety.status,
                reason=hnsw_safety.message,
            )
        data = search_memories(
            query=query,
            palace_path=self._palace,
            wing=wing,
            room=room,
            n_results=n_results,
            vector_disabled=hnsw_safety.vector_disabled,
        )
        if isinstance(data, dict) and data.get("error"):
            raise MemoryBackendUnavailable(str(data.get("error")))
        records = parse_search_tool_payload(
            data,
            default_memory_space_id=self._memory_space_id,
        )
        return apply_recall_policy(
            self._hydrate_hit_metadata(records),
            self._settings,
        )

    async def search_scoped(
        self,
        query: str,
        *,
        wings: list[str],
        n_results: int = 5,
        room: str | None = None,
        skip_closets: bool = False,
    ) -> list[MemoryWireRecord]:
        """Adapter-owned multi-wing search with one query embedding."""
        return await asyncio.to_thread(
            self.search_scoped_sync,
            query,
            wings=wings,
            n_results=n_results,
            room=room,
            skip_closets=skip_closets,
        )

    def search_scoped_sync(
        self,
        query: str,
        *,
        wings: list[str],
        n_results: int = 5,
        room: str | None = None,
        skip_closets: bool = False,
    ) -> list[MemoryWireRecord]:
        raw = search_memories_shared_embedding(
            query,
            self._palace,
            wings=wings,
            room=room,
            n_results=n_results,
            skip_closets=skip_closets,
        )
        records = parse_search_tool_payload(
            {"results": raw},
            default_memory_space_id=self._memory_space_id,
        )
        # Unlike MemPalace's public search payload, the scoped adapter reads
        # Chroma's stored metadata directly, so privacy/provenance are already
        # present and no second collection.get hydration round is required.
        return apply_recall_policy(records, self._settings)

    def _hydrate_hit_metadata(
        self,
        records: list[MemoryWireRecord],
    ) -> list[MemoryWireRecord]:
        """Batch-load metadata only for current hits, independent of Realm size.

        MemPalace 3.5's public search payload drops drawer IDs and custom
        metadata. Eidolon-owned drawers have deterministic IDs, so one bounded
        ``get(ids=...)`` restores privacy/provenance without caching every
        archived drawer accumulated over the user's lifetime.
        """
        if not records:
            return []
        try:
            collection = _get_collection(self._palace, create=False)
            expected = [
                (
                    _drawer_id(
                        str(record.metadata.get("wing") or ""),
                        str(record.metadata.get("room") or record.key or ""),
                        str(
                            record.metadata.pop(
                                "_raw_search_text",
                                _drawer_content_text(record.value),
                            )
                        ),
                    ),
                    record,
                )
                for record in records
            ]
            result = collection.get(
                ids=list(dict.fromkeys(drawer_id for drawer_id, _record in expected)),
                include=["metadatas"],
            )
            ids = _ids(result)
            metadatas = _metadatas(result)
            if len(ids) != len(metadatas):
                raise MemoryBackendUnavailable("hit metadata query returned inconsistent rows")
            stored = dict(zip(ids, metadatas, strict=True))
            hydrated: list[MemoryWireRecord] = []
            for drawer_id, record in expected:
                metadata = stored.get(drawer_id)
                if metadata is None:
                    log.warning(
                        "mempalace_hit_metadata_unverified",
                        drawer_id=drawer_id,
                        wing=record.metadata.get("wing"),
                        room=record.metadata.get("room"),
                    )
                    record.metadata = {
                        **record.metadata,
                        "_storage_metadata_verified": False,
                    }
                else:
                    record.metadata = {
                        **record.metadata,
                        **metadata,
                        "_storage_metadata_verified": True,
                    }
                hydrated.append(record)
            return hydrated
        except Exception as exc:
            # Privacy policy is fail-closed: metadata verification failure must
            # degrade recall, never expose a potentially archived drawer.
            raise MemoryBackendUnavailable(
                f"failed to verify memory hit metadata: {exc}"
            ) from exc

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

        now_iso = _now_iso()
        raw_meta = dict(metadata or {})
        occurred_at = str(raw_meta.get("occurred_at") or raw_meta.get("memory_time") or now_iso)
        raw_meta["occurred_at"] = occurred_at
        raw_meta.setdefault("indexed_at", now_iso)
        # MemPalace's public search currently exposes ``filed_at`` as the
        # top-level ``created_at`` and drops custom metadata. Store the
        # canonical memory time here so plain search still reports when the
        # topic happened; ``indexed_at`` keeps the physical write time.
        raw_meta["filed_at"] = occurred_at
        drawer_id = _drawer_id(wing, room, content)
        meta = _metadata_for_chroma(
            {
                **raw_meta,
                "wing": wing,
                "room": room,
                "source_file": raw_meta.get("source_file", ""),
                "chunk_index": 0,
                "added_by": raw_meta.get("added_by", "eidolon-memory"),
            }
        )
        try:
            existing = collection.get(ids=[drawer_id], include=[])
            if _ids(existing):
                return
            upsert_kwargs: dict[str, Any] = {
                "ids": [drawer_id],
                "documents": [content],
                "metadatas": [meta],
            }
            if selected_mempalace_backend(self._settings) == "sqlite_exact":
                upsert_kwargs["embeddings"] = [_deterministic_embedding(content)]
            collection.upsert(**upsert_kwargs)
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
            "memory_id": fragment.memory_id,
            "memory_space_id": fragment.memory_space_id,
            "memory_realm_id": fragment.memory_realm_id or fragment.memory_space_id,
            "owner_id": fragment.owner_id or "",
            "companion_id": fragment.companion_id or "",
            "scope": fragment.scope,
            "visibility": fragment.visibility,
            "source_device_id": fragment.source_device_id or "",
            "target_device_id": fragment.target_device_id or "",
            "source_instance_id": fragment.source_instance_id or "",
            "source_companion_id": fragment.companion_id or fragment.source_instance_id or "",
            "source_turn_id": fragment.source_turn_id,
            "schema_version": "2",
            "session_id": fragment.session_id or "",
            "importance": fragment.importance,
            "confidence": fragment.confidence,
            "memory_type": fragment.memory_type,
            "privacy": fragment.privacy,
            "tags": fragment.tags,
            "extensions": fragment.extensions,
        }
        if fragment.occurred_at:
            metadata["occurred_at"] = fragment.occurred_at
        await self.ingest_text(
            wing=fragment.wing,
            room=fragment.room,
            text=fragment.content,
            metadata=metadata,
        )

    async def get(self, memory_space_id: str, key: str) -> MemoryWireRecord | None:
        del memory_space_id
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
        memory_space_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        """List drawers filtered by memory space, or enumerate the palace when blank.

        Blank ``memory_space_id``: return a page of all drawers in the collection (no ``where``
        clause; order is backend-defined).
        """
        try:
            collection = _get_collection(self._palace, create=False)
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc

        tenant = memory_space_id.strip()
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

        tenant_where: dict[str, Any] = {"memory_space_id": tenant}

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

    async def get_by_source_turn_id(
        self,
        memory_space_id: str,
        source_turn_id: str,
    ) -> MemoryWireRecord | None:
        tenant = memory_space_id.strip()
        turn = source_turn_id.strip()
        if not tenant or not turn:
            return None
        try:
            collection = _get_collection(self._palace, create=False)
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc

        try:
            result = collection.get(
                where={
                    "$and": [
                        {"memory_space_id": tenant},
                        {"source_turn_id": turn},
                    ]
                },
                include=["documents", "metadatas"],
                limit=1,
            )
            ids = _ids(result)
            if ids:
                return _record_from_get_result(result, 0, drawer_id=ids[0])
        except Exception:
            pass

        try:
            result = collection.get(
                where={"source_turn_id": turn},
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            raise MemoryBackendUnavailable(str(exc)) from exc
        for idx, drawer_id in enumerate(_ids(result)):
            rec = _record_from_get_result(result, idx, drawer_id=drawer_id)
            if rec.memory_space_id == tenant or rec.metadata.get("memory_space_id") == tenant:
                return rec
        return None

    async def delete(self, memory_space_id: str, key: str) -> None:
        await self.delete_many(memory_space_id, [key])

    async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        unique_ids = _validated_privacy_drawer_ids(keys)
        try:
            collection = _get_collection(self._palace, create=False)
            existing = collection.get(ids=unique_ids, include=["documents", "metadatas"])
            _assert_privacy_batch_tenant(
                existing,
                memory_space_id=memory_space_id,
                authoritative_space_id=self._memory_space_id,
            )
            collection.delete(ids=unique_ids)
            remaining = collection.get(ids=unique_ids, include=[])
            if _ids(remaining):
                msg = f"drawers remain visible after delete: {_ids(remaining)!r}"
                raise MemoryBackendWriteFailed(msg)
            return unique_ids
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except MemoryBackendWriteFailed:
            raise
        except Exception as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc

    async def archive_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        unique_ids = _validated_privacy_drawer_ids(keys)
        try:
            collection = _get_collection(self._palace, create=False)
            existing = collection.get(ids=unique_ids, include=["documents", "metadatas"])
            _assert_privacy_batch_tenant(
                existing,
                memory_space_id=memory_space_id,
                authoritative_space_id=self._memory_space_id,
            )
            existing_ids = _ids(existing)
            if not existing_ids:
                return []
            metadata_by_id = dict(zip(existing_ids, _metadatas(existing), strict=False))
            archived_at = _now_iso()
            updated = [
                _metadata_for_chroma(
                    {
                        **metadata_by_id.get(drawer_id, {}),
                        "privacy": "do_not_recall",
                        "archived_at": archived_at,
                        "updated_at": archived_at,
                    }
                )
                for drawer_id in existing_ids
            ]
            collection.update(ids=existing_ids, metadatas=updated)

            verified = collection.get(ids=existing_ids, include=["metadatas"])
            verified_by_id = dict(
                zip(_ids(verified), _metadatas(verified), strict=False)
            )
            failed = [
                drawer_id
                for drawer_id in existing_ids
                if str(verified_by_id.get(drawer_id, {}).get("privacy"))
                != "do_not_recall"
            ]
            if failed:
                msg = f"drawers remain recallable after archive: {failed!r}"
                raise MemoryBackendWriteFailed(msg)
            return existing_ids
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except MemoryBackendWriteFailed:
            raise
        except Exception as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc


def _merge_tenant_queries(collection: Any, *, tenant_id: str) -> list[MemoryWireRecord]:
    """Fallback for old Chroma versions that reject the primary ``where`` call."""
    by_id: dict[str, MemoryWireRecord] = {}

    try:
        u = collection.get(
            where={"memory_space_id": tenant_id},
            include=["documents", "metadatas"],
        )
        for idx, drawer_id in enumerate(_ids(u)):
            by_id[drawer_id] = _record_from_get_result(u, idx, drawer_id=drawer_id)
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
        if hit.metadata.get("_storage_metadata_verified") is False:
            continue
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


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _deterministic_embedding(text: str, *, dim: int = 64) -> list[float]:
    """Small offline embedding for sqlite_exact benchmark/dev storage."""
    vector = [0.0] * dim
    tokens = _TOKEN_RE.findall((text or "").lower()) or [text or ""]
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        idx = digest[0] % dim
        sign = 1.0 if digest[1] % 2 == 0 else -1.0
        vector[idx] += sign * (1.0 + digest[2] / 255.0)
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


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


def _drawer_content_text(value: Any) -> str:
    """Recover deterministic drawer content from parsed search values."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value or "")


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


def _validated_privacy_drawer_ids(keys: list[str]) -> list[str]:
    unique_ids = list(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))
    if not unique_ids or any(not key.startswith("drawer_") for key in unique_ids):
        msg = "privacy mutation expects one or more MemPalace drawer_id keys"
        raise MemoryBackendUnsupported(msg)
    return unique_ids


def _assert_privacy_batch_tenant(
    result: Any,
    *,
    memory_space_id: str,
    authoritative_space_id: str | None,
) -> None:
    expected = memory_space_id.strip()
    if not expected:
        raise MemoryBackendWriteFailed("memory_space_id is required for privacy mutation")
    drawer_ids = _ids(result)
    metadatas = _metadatas(result)
    if len(metadatas) != len(drawer_ids) or any(
        not isinstance(metadata, dict) for metadata in metadatas
    ):
        raise MemoryBackendWriteFailed("cannot verify drawer tenant metadata")
    for drawer_id, metadata in zip(drawer_ids, metadatas, strict=True):
        stored = str(metadata.get("memory_space_id") or authoritative_space_id or "").strip()
        if not stored:
            msg = f"cannot verify drawer tenant before privacy mutation: {drawer_id}"
            raise MemoryBackendWriteFailed(msg)
        if stored != expected:
            msg = f"drawer belongs to another memory space: {drawer_id}"
            raise MemoryBackendWriteFailed(msg)


def _record_from_get_result(result: Any, index: int, *, drawer_id: str) -> MemoryWireRecord:
    docs = _documents(result)
    metas = _metadatas(result)
    meta = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
    content = docs[index] if index < len(docs) else ""
    wing = str(meta.get("wing", "default"))
    room = str(meta.get("room", drawer_id))
    memory_space_id = str(meta.get("memory_space_id") or wing)
    return MemoryWireRecord(
        memory_space_id=memory_space_id,
        key=drawer_id or room,
        value=content,
        # Preserve the *stored* ``source`` (e.g. "user-confirmed",
        # "consolidator") — it's the write-time provenance that recall
        # ranking + theme rendering key off. Only default to
        # "mempalace-python" when the drawer carried no source at all.
        # ``wing``/``room`` stay authoritative (read-time placement).
        metadata={"source": "mempalace-python", **meta, "wing": wing, "room": room},
        created_at=parse_memory_datetime(meta.get("created_at") or meta.get("filed_at")),
        updated_at=parse_memory_datetime(meta.get("updated_at")),
    )


def _nested(result: Any, name: str) -> list[Any]:
    if isinstance(result, dict):
        value = result.get(name) or []
    else:
        value = getattr(result, name, []) or []
    if value and isinstance(value[0], list):
        return list(value[0])
    return list(value)


def _records_from_query_result(result: Any) -> list[MemoryWireRecord]:
    ids = [str(v) for v in _nested(result, "ids")]
    docs = [str(v) for v in _nested(result, "documents")]
    metas = _nested(result, "metadatas")
    distances = _nested(result, "distances")
    records: list[MemoryWireRecord] = []
    for idx, drawer_id in enumerate(ids):
        meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
        if idx < len(distances):
            try:
                distance = float(distances[idx])
                meta = {**meta, "distance": distance, "similarity": max(0.0, 1.0 - distance)}
            except (TypeError, ValueError):
                pass
        content = docs[idx] if idx < len(docs) else ""
        wing = str(meta.get("wing", "default"))
        room = str(meta.get("room", drawer_id))
        records.append(
            MemoryWireRecord(
                memory_space_id=str(meta.get("memory_space_id") or wing),
                key=drawer_id or room,
                value=content,
                metadata={"source": "mempalace-python", **meta, "wing": wing, "room": room},
                created_at=parse_memory_datetime(meta.get("created_at") or meta.get("filed_at")),
                updated_at=parse_memory_datetime(meta.get("updated_at")),
            )
        )
    return records
