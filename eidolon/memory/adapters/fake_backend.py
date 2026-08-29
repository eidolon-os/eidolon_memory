"""In-memory backend for unit tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.wire import MemoryWireRecord


class FakeMemoryBackend:
    """Simple async dict + list store mimicking vector hits.

    ``lock`` is None per MemoryBackend Protocol — no concurrency state to
    serialize for tests. Production backends that need single-owner state
    (LockedBackend) override this with a real ``asyncio.Lock``.
    """

    lock: asyncio.Lock | None = None
    working_memory: Any = None  # Phase 2 ring; None for unit-test fakes

    def __init__(self) -> None:
        self.docs: dict[str, MemoryWireRecord] = {}
        self.ingests: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.searches: list[tuple[str, str, int, str | None]] = []

    def _doc_id(self, memory_space_id: str, key: str) -> str:
        return f"{memory_space_id}::{key}"

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
        audiences: tuple[str, ...] | None = None,
    ) -> list[MemoryWireRecord]:
        self.searches.append((query, wing, n_results, room))
        hits: list[MemoryWireRecord] = []
        q = query.lower()
        for rec in self.docs.values():
            if rec.metadata.get("wing", rec.memory_space_id) != wing:
                continue
            if room and rec.key != room:
                continue
            if audiences is not None and str(
                rec.metadata.get("audience") or "owner"
            ) not in audiences:
                continue
            blob = json.dumps(rec.value, ensure_ascii=False).lower() if rec.value else ""
            if q in blob or q in rec.key.lower():
                hits.append(rec)
        return hits[:n_results]

    async def ingest_text(
        self,
        *,
        wing: str,
        room: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.ingests.append((wing, room, text, metadata))
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Default ``source="fake"`` only when the caller didn't set one — don't
        # silently discard caller-provided metadata (e.g. Phase 5.2 writes
        # ``source="user-confirmed"``, which recall ranking keys off). ``wing``
        # and ``room`` remain authoritative (the adapter owns placement).
        raw_meta = dict(metadata or {})
        occurred_at = str(raw_meta.get("occurred_at") or raw_meta.get("memory_time") or now_iso)
        raw_meta["occurred_at"] = occurred_at
        raw_meta.setdefault("indexed_at", now_iso)
        raw_meta.setdefault("filed_at", occurred_at)
        meta = {"source": "fake", **raw_meta, "wing": wing, "room": room}
        did = self._doc_id(str(raw_meta.get("memory_space_id") or wing), room)
        self.docs[did] = MemoryWireRecord(
            memory_space_id=str(meta.get("memory_space_id") or wing),
            key=room,
            value=text,
            metadata=meta,
        )

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        meta = {
            **fragment.metadata,
            "memory_id": fragment.memory_id,
            "memory_space_id": fragment.memory_space_id,
            "memory_realm_id": fragment.memory_realm_id or fragment.memory_space_id,
            "owner_id": fragment.owner_id or "",
            "companion_id": fragment.companion_id or "",
            # Must mirror the real adapter: this backend stands in for it in
            # tests, so a field it silently drops is a rule those tests cannot
            # check. Audience is exactly such a field.
            "audience": fragment.audience,
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
            meta["occurred_at"] = fragment.occurred_at
        await self.ingest_text(
            wing=fragment.wing,
            room=fragment.room,
            text=fragment.content,
            metadata=meta,
        )

    async def ingest_fragments(self, fragments: Sequence[MemoryFragment]) -> None:
        """Must exist, or the tests stop exercising the path production takes.

        ``LockedBackend`` falls back to a loop when its inner store has no batch
        write. That fallback is correct, so a fake without this method would pass
        every test while the batching under measurement never ran.
        """

        for fragment in fragments:
            await self.ingest_fragment(fragment)

    async def get(self, memory_space_id: str, key: str) -> MemoryWireRecord | None:
        did = self._doc_id(memory_space_id, key)
        return self.docs.get(did)

    async def get_many(
        self, memory_space_id: str, keys: list[str]
    ) -> list[MemoryWireRecord]:
        """Missing ids omitted, matching the real store rather than the loop."""

        found = (self.docs.get(self._doc_id(memory_space_id, key)) for key in keys)
        return [record for record in found if record is not None]

    async def get_all(
        self,
        memory_space_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        if not memory_space_id.strip():
            items = sorted(
                self.docs.values(),
                key=lambda r: (
                    str(r.metadata.get("wing", r.memory_space_id)),
                    str(r.key),
                ),
            )
            sliced = items[offset or 0 :]
            if limit is not None:
                sliced = sliced[:limit]
            return sliced

        filtered = [
            r
            for r in self.docs.values()
            if r.metadata.get("memory_space_id") == memory_space_id
            or r.memory_space_id == memory_space_id
        ]
        sliced = filtered[offset or 0 :]
        if limit is not None:
            sliced = sliced[:limit]
        return sliced

    async def get_by_source_turn_id(
        self,
        memory_space_id: str,
        source_turn_id: str,
    ) -> MemoryWireRecord | None:
        for rec in self.docs.values():
            if rec.metadata.get("memory_space_id") != memory_space_id and (
                rec.memory_space_id != memory_space_id
            ):
                continue
            if rec.metadata.get("source_turn_id") == source_turn_id:
                return rec
        return None

    async def delete(self, memory_space_id: str, key: str) -> None:
        await self.delete_many(memory_space_id, [key])

    async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        unique = list(dict.fromkeys(keys))
        for key in unique:
            self.docs.pop(self._doc_id(memory_space_id, key), None)
        return unique

    async def archive_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
        archived: list[str] = []
        for key in dict.fromkeys(keys):
            did = self._doc_id(memory_space_id, key)
            record = self.docs.get(did)
            if record is None:
                continue
            record.metadata = {
                **record.metadata,
                "privacy": "do_not_recall",
            }
            archived.append(key)
        return archived
