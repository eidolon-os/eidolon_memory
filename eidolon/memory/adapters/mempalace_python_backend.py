"""MemPalace integration through its in-process Python API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mempalace.backends.base import GetResult

from eidolon.memory.adapters.mempalace_fast_search import search_memories_shared_embedding
from eidolon.memory.adapters.mempalace_results import storage_record
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import (
    MemoryBackendUnavailable,
    MemoryBackendUnsupported,
    MemoryBackendWriteFailed,
)
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.room_graph import RoomGraphSnapshot, RoomNode
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.embedder_factory import active_embedder
from eidolon.memory.infrastructure.mempalace_backend import selected_mempalace_backend
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
        self._memory_space_id = memory_space_id

    async def room_graph(self) -> RoomGraphSnapshot | None:
        """Every room in this palace, with the wings each appears under.

        Unserialised, like every other method here: this adapter is meant to be
        wrapped by ``LockedBackend``, which is what keeps Chroma's SQLite-backed
        cursor off the write path.
        """

        def _read() -> RoomGraphSnapshot | None:
            from mempalace.config import MempalaceConfig
            from mempalace.palace import get_collection
            from mempalace.palace_graph import build_graph, graph_stats

            collection = get_collection(self._palace, create=False, read_only=True)
            if collection is None:
                return None
            config = MempalaceConfig(palace_path=self._palace)
            raw_nodes, _raw_edges = build_graph(col=collection, config=config)
            return RoomGraphSnapshot(
                rooms={
                    room: RoomNode(
                        wings=tuple(data.get("wings") or ()),
                        halls=tuple(data.get("halls") or ()),
                        count=int(data.get("count") or 0),
                    )
                    for room, data in raw_nodes.items()
                },
                stats=graph_stats(col=collection, config=config),
            )

        return await asyncio.to_thread(_read)

    async def warm_read_path(self, *, wings: Sequence[str]) -> None:
        """Pay the first-read cost now: ONNX session, closets handle, one search.

        Only embedded storage benefits. With a vector server the index and its
        caches live on the server, already warm, and the model load is the one
        cost a dry-run search here would not avoid anyway — so this returns
        without doing the work rather than warming something remote.

        Deciding that here rather than in the caller is the point: which stores
        are local is this adapter's own knowledge, and it changes when MemPalace
        adds a backend, not when memory's startup sequence changes.
        """

        backend = selected_mempalace_backend(self._settings)
        if backend != "chroma":
            log.info("warm_read_path_skipped_remote_store", backend=backend, palace=self._palace)
            return
        await asyncio.to_thread(self._warm_read_path_sync, tuple(wings))

    def _warm_read_path_sync(self, wings: tuple[str, ...]) -> None:
        log.info("warm_embedding_start", palace=self._palace)
        if self._settings.mempalace.offline_embedding:
            # Test mode deliberately has no configured model process.  Warm the
            # exact public Chroma path used by a test recall, including its
            # configured-width explicit vector; asking MemPalace to embed here
            # would silently load minilm (384d) beside our 512d collection.
            self.search_scoped_sync(
                "warmup",
                wings=list(wings),
                n_results=1,
                skip_closets=True,
            )
            log.info("warm_complete", palace=self._palace, wings=len(wings))
            return

        # Through the port, and on the query side, because what this is warming is
        # the read path: a first call pays the model load, and for a hosted
        # embedder it also opens the connection. Warming the document side would
        # leave the query-side prefix cold, which for E5 is a different code path.
        active_embedder().embed_queries(["eidolon memory warmup"])

        for wing_id in wings:
            try:
                search_memories_shared_embedding(
                    "warmup",
                    self._palace,
                    wings=[wing_id],
                    room=None,
                    n_results=1,
                    skip_closets=True,
                )
                log.info("warm_search_ok", wing=wing_id)
            except MemoryBackendUnavailable as exc:
                log.warning("warm_search_failed", wing=wing_id, error=str(exc))

        log.info("warm_complete", palace=self._palace, wings=len(wings))

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
        audiences: tuple[str, ...] | None = None,
        device_id: str | None = None,
    ) -> list[MemoryWireRecord]:
        return await asyncio.to_thread(
            self.search_sync,
            query,
            wing=wing,
            n_results=n_results,
            room=room,
            audiences=audiences,
            device_id=device_id,
        )

    def search_sync(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
        audiences: tuple[str, ...] | None = None,
        device_id: str | None = None,
    ) -> list[MemoryWireRecord]:
        return self.search_scoped_sync(
            query,
            wings=[wing],
            n_results=n_results,
            room=room,
            audiences=audiences,
            device_id=device_id,
        )

    async def search_scoped(
        self,
        query: str,
        *,
        wings: list[str],
        n_results: int = 5,
        room: str | None = None,
        audiences: tuple[str, ...] | None = None,
        device_id: str | None = None,
        skip_closets: bool = False,
        diagnostics: dict[str, float] | None = None,
    ) -> list[MemoryWireRecord]:
        """Adapter-owned multi-wing search with one query embedding."""
        return await asyncio.to_thread(
            self.search_scoped_sync,
            query,
            wings=wings,
            n_results=n_results,
            room=room,
            audiences=audiences,
            device_id=device_id,
            skip_closets=skip_closets,
            diagnostics=diagnostics,
        )

    def search_scoped_sync(
        self,
        query: str,
        *,
        wings: list[str],
        n_results: int = 5,
        room: str | None = None,
        audiences: tuple[str, ...] | None = None,
        device_id: str | None = None,
        skip_closets: bool = False,
        diagnostics: dict[str, float] | None = None,
    ) -> list[MemoryWireRecord]:
        embedding = None
        if self._settings.mempalace.offline_embedding:
            embedding = _deterministic_embedding(query, dim=_offline_embedding_dim(self._settings))
        records = search_memories_shared_embedding(
            query,
            self._palace,
            wings=wings,
            room=room,
            audiences=audiences,
            device_id=device_id,
            n_results=n_results,
            skip_closets=skip_closets,
            query_embedding=embedding,
            diagnostics=diagnostics,
            memory_space_id=self._memory_space_id,
            lexical_candidates=self._settings.recall.rerank_enabled,
        )
        return apply_recall_policy(records, self._settings)

    async def aclose(self) -> None:
        """Release this palace through MemPalace's public lifecycle API."""

        def close() -> None:
            from mempalace.palace import get_backend_for_palace

            get_backend_for_palace(self._palace).close_palace(self._palace)

        await asyncio.to_thread(close)

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._write_drawers([self._prepare_drawer(wing, room, text, metadata)])

    def _prepare_drawer(
        self,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None,
    ) -> tuple[str, str, dict[str, Any]]:
        """Validate and shape one drawer. Pure — no store access, no clock skew.

        Split out so a single write and a whole turn's batch build their rows the
        same way rather than by two code paths that must be kept identical.
        """

        try:
            wing = _sanitize_name(wing, "wing")
            room = _sanitize_name(room, "room")
            content = _sanitize_content(text)
        except ValueError as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc

        now_iso = _now_iso()
        raw_meta = dict(metadata or {})
        occurred_at = str(raw_meta.get("occurred_at") or raw_meta.get("memory_time") or now_iso)
        raw_meta["occurred_at"] = occurred_at
        raw_meta.setdefault("indexed_at", now_iso)
        # Respect MemPalace's filing-time contract. Event time stays separate;
        # MemoryWireRecord derives it centrally for the Owner's day list.
        raw_meta["filed_at"] = raw_meta["indexed_at"]
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
        return _drawer_id(wing, room, content), content, meta

    async def _write_drawers(self, rows: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Write any number of drawers in one pass over the store.

        Three Chroma calls regardless of how many rows: one ``get`` to find which
        ids are already present, one ``upsert``, one ``get`` to confirm the write is
        readable. Per fragment that was three calls each — eighteen for a
        six-fragment turn — and measured on a real palace six one-document upserts
        cost 32 ms against 10.6 ms for one six-document upsert. The embedding is not
        what dominates: it is 1.2 ms of a 9 ms write.

        Run off the event loop. These are blocking calls that used to execute
        directly inside an ``async def``, so a turn's writes stalled everything else
        in the process for their whole duration — including, once one process serves
        several spaces, other spaces' recalls.
        """

        if not rows:
            return
        try:
            collection = _get_write_collection(self._palace, create=True)
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc

        # Two fragments of a turn can sanitise to identical content — the id is a
        # hash of (wing, room, content) — and Chroma rejects a batch whose ids are
        # not unique. Deduplicated here rather than discovered as a failed turn.
        unique: dict[str, tuple[str, str, dict[str, Any]]] = {}
        for drawer_id, content, meta in rows:
            unique.setdefault(drawer_id, (drawer_id, content, meta))

        await asyncio.to_thread(self._write_drawers_sync, collection, list(unique.values()))

    def _write_drawers_sync(
        self,
        collection: Any,
        rows: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        try:
            present = set(collection.get(ids=[r[0] for r in rows], include=[]).ids)
            fresh = [r for r in rows if r[0] not in present]
            if not fresh:
                return

            upsert_kwargs: dict[str, Any] = {
                "ids": [r[0] for r in fresh],
                "documents": [r[1] for r in fresh],
                "metadatas": [r[2] for r in fresh],
            }
            if self._settings.mempalace.offline_embedding:
                dim = _offline_embedding_dim(self._settings)
                upsert_kwargs["embeddings"] = [
                    _deterministic_embedding(r[1], dim=dim) for r in fresh
                ]
            else:
                embeddings = active_embedder().embed_documents([r[1] for r in fresh])
                if len(embeddings) != len(fresh) or any(not row for row in embeddings):
                    raise MemoryBackendWriteFailed(
                        "configured embedder returned an incomplete document batch"
                    )
                upsert_kwargs["embeddings"] = [
                    [float(value) for value in row] for row in embeddings
                ]
            collection.upsert(**upsert_kwargs)

            written = set(collection.get(ids=[r[0] for r in fresh], include=[]).ids)
            missing = [r[0] for r in fresh if r[0] not in written]
            if missing:
                msg = (
                    f"MemPalace acknowledged the write but {len(missing)} of "
                    f"{len(fresh)} drawers are not readable"
                )
                raise MemoryBackendWriteFailed(msg)
        except MemoryBackendWriteFailed:
            raise
        except Exception as exc:
            raise MemoryBackendWriteFailed(str(exc)) from exc

    async def ingest_fragments(self, fragments: Sequence[MemoryFragment]) -> None:
        await self._write_drawers(
            [
                self._prepare_drawer(f.wing, f.room, f.content, self._fragment_metadata(f))
                for f in fragments
            ]
        )

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        await self.ingest_fragments([fragment])

    def _fragment_metadata(self, fragment: MemoryFragment) -> dict[str, Any]:
        metadata = {
            **fragment.metadata,
            "memory_id": fragment.memory_id,
            "memory_space_id": fragment.memory_space_id,
            "memory_realm_id": fragment.memory_realm_id or fragment.memory_space_id,
            "owner_id": fragment.owner_id or "",
            "companion_id": fragment.companion_id or "",
            # Visibility, distinct from the provenance above it. Stored as a
            # top-level key rather than nested so both stores can push the filter
            # down: chroma takes it in a where clause, and milvus's dynamic
            # fields make it a queryable column.
            "audience": fragment.audience,
            "scope": fragment.scope,
            "visibility": fragment.visibility,
            "source_device_id": fragment.source_device_id or "",
            "target_device_id": fragment.target_device_id or "",
            "source_instance_id": fragment.source_instance_id or "",
            "source_companion_id": fragment.companion_id or fragment.source_instance_id or "",
            "source_turn_id": fragment.source_turn_id,
            "evidence_quote": fragment.evidence_quote,
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
        return metadata

    async def get(self, memory_space_id: str, key: str) -> MemoryWireRecord | None:
        del memory_space_id
        try:
            collection = _get_read_collection(self._palace)
            result = collection.get(ids=[key], include=["documents", "metadatas"])
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except Exception as exc:
            raise MemoryBackendUnavailable(str(exc)) from exc
        if not result.ids:
            return None
        return _record_from_get_result(result, 0, drawer_id=key)

    async def get_many(self, memory_space_id: str, keys: list[str]) -> list[MemoryWireRecord]:
        """Fetch a batch of drawers in one call.

        Chroma's ``get`` has always taken a list of ids; ``get`` above passes a
        list of one. A privacy command carries up to a hundred drawer ids and the
        forget path has to read every one before deleting it, which as a loop over
        ``get`` was a hundred round trips each taking the space's read lock — on a
        Pi, the difference between imperceptible and noticeable, for no reason
        other than the port never having offered the plural.

        Missing ids are omitted rather than returned as gaps. Callers here are
        reconciling against ids they already hold, so position carries no meaning
        and a shorter list is the honest answer.
        """

        del memory_space_id
        wanted = list(dict.fromkeys(key.strip() for key in keys if key.strip()))
        if not wanted:
            return []
        try:
            collection = _get_read_collection(self._palace)
            result = collection.get(ids=wanted, include=["documents", "metadatas"])
        except ImportError as exc:
            raise MemoryBackendUnavailable("mempalace package is not installed") from exc
        except Exception as exc:
            raise MemoryBackendUnavailable(str(exc)) from exc
        found = result.ids
        return [
            _record_from_get_result(result, index, drawer_id=drawer_id)
            for index, drawer_id in enumerate(found)
        ]

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
            collection = _get_read_collection(self._palace)
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
                    for idx, drawer_id in enumerate(result.ids)
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
                for idx, drawer_id in enumerate(result.ids)
            ]
        except Exception as exc:
            raise MemoryBackendUnavailable(str(exc)) from exc

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
            collection = _get_read_collection(self._palace)
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
            ids = result.ids
            if ids:
                return _record_from_get_result(result, 0, drawer_id=ids[0])
        except Exception as exc:
            raise MemoryBackendUnavailable(str(exc)) from exc
        return None

    async def delete(self, memory_space_id: str, key: str) -> None:
        await self.delete_many(memory_space_id, [key])

    async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        unique_ids = _validated_privacy_drawer_ids(keys)
        try:
            collection = _get_write_collection(self._palace, create=False)
            existing = collection.get(ids=unique_ids, include=["documents", "metadatas"])
            _assert_privacy_batch_tenant(
                existing,
                memory_space_id=memory_space_id,
                authoritative_space_id=self._memory_space_id,
            )
            collection.delete(ids=unique_ids)
            remaining = collection.get(ids=unique_ids, include=[])
            if remaining.ids:
                msg = f"drawers remain visible after delete: {remaining.ids!r}"
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
            collection = _get_write_collection(self._palace, create=False)
            existing = collection.get(ids=unique_ids, include=["documents", "metadatas"])
            _assert_privacy_batch_tenant(
                existing,
                memory_space_id=memory_space_id,
                authoritative_space_id=self._memory_space_id,
            )
            existing_ids = existing.ids
            if not existing_ids:
                return []
            metadata_by_id = dict(zip(existing_ids, existing.metadatas, strict=False))
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
            verified_by_id = dict(zip(verified.ids, verified.metadatas, strict=False))
            failed = [
                drawer_id
                for drawer_id in existing_ids
                if str(verified_by_id.get(drawer_id, {}).get("privacy")) != "do_not_recall"
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


def apply_recall_policy(
    hits: list[MemoryWireRecord],
    settings: MemorySettings,
) -> list[MemoryWireRecord]:
    """Filter private/archived candidates; final top-k belongs to recall ranking."""
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
    return filtered


def _get_read_collection(palace_path: str):
    from mempalace.palace import get_collection

    return get_collection(palace_path, create=False, read_only=True)


def _get_write_collection(palace_path: str, *, create: bool):
    from mempalace.palace import get_collection

    return get_collection(palace_path, create=create)


def _sanitize_name(value: str, field_name: str) -> str:
    from mempalace.config import sanitize_name

    return sanitize_name(value, field_name)


def _sanitize_content(value: str) -> str:
    from mempalace.config import sanitize_content

    return sanitize_content(value)


def _drawer_id(wing: str, room: str, content: str) -> str:
    digest = hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]
    return f"drawer_{wing}_{room}_{digest}"


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


#: Fallback width, used only when the configured embedder's dimension is unknown —
#: which now means a model name MemPalace resolves for itself and we do not
#: recognise, since it answers those with minilm at 384. It was once a bare
#: constant on the grounds that both of MemPalace's own embedders emit 384, but a
#: palace's collection is created with the *configured* embedder's width, and ours
#: run from 384 to 1024. A hash vector of any other width is rejected on the first
#: write, so the width has to follow the configuration.
_OFFLINE_EMBEDDING_DIM = 384


def _offline_embedding_dim(settings: MemorySettings) -> int:
    """The width the collection will have been created with.

    Asked of the embedding configuration rather than looked up in one
    implementation's model table: a hosted embedder declares its width too, and a
    mismatch is not a degraded result — Chroma rejects the write outright.
    """

    identity = settings.embedding.declared_identity()
    return identity.dimension if identity is not None else _OFFLINE_EMBEDDING_DIM


def _deterministic_embedding(text: str, *, dim: int = _OFFLINE_EMBEDDING_DIM) -> list[float]:
    """Hash text into a vector, for tests and benchmarks only.

    Lets a test drive the real storage adapter without loading the embedder.
    Semantically meaningless — two related sentences land nowhere near each
    other — so it is only ever appropriate where ranking is not what is under
    test. Enabled by ``mempalace.offline_embedding``.
    """
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
    drawer_ids = result.ids
    metadatas = result.metadatas
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


def _record_from_get_result(result: GetResult, index: int, *, drawer_id: str) -> MemoryWireRecord:
    if len(result.ids) != len(result.documents) or len(result.ids) != len(result.metadatas):
        raise MemoryBackendUnavailable("misaligned MemPalace get result")
    return storage_record(
        drawer_id,
        result.documents[index],
        result.metadatas[index],
        search=False,
    )
