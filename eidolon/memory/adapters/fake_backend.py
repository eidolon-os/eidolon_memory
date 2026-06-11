"""In-memory backend for unit tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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

    def _doc_id(self, user_id: str, key: str) -> str:
        return f"{user_id}::{key}"

    async def search(
        self,
        query: str,
        *,
        wing: str,
        n_results: int = 5,
        room: str | None = None,
    ) -> list[MemoryWireRecord]:
        self.searches.append((query, wing, n_results, room))
        hits: list[MemoryWireRecord] = []
        q = query.lower()
        for rec in self.docs.values():
            if rec.metadata.get("wing", rec.user_id) != wing:
                continue
            if room and rec.key != room:
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
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Default ``source="fake"`` only when the caller didn't set one — don't
        # silently discard caller-provided metadata (e.g. Phase 5.2 writes
        # ``source="user-confirmed"``, which recall ranking keys off). ``wing``
        # and ``room`` remain authoritative (the adapter owns placement).
        raw_meta = dict(metadata or {})
        raw_meta.setdefault("occurred_at", raw_meta.get("memory_time") or now_iso)
        raw_meta.setdefault("filed_at", now_iso)
        meta = {"source": "fake", **raw_meta, "wing": wing, "room": room}
        did = self._doc_id(wing, room)
        self.docs[did] = MemoryWireRecord(
            user_id=wing,
            key=room,
            value=text,
            metadata=meta,
        )

    async def ingest_fragment(self, fragment: MemoryFragment) -> None:
        meta = {
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
            meta["occurred_at"] = fragment.occurred_at
        await self.ingest_text(
            wing=fragment.wing,
            room=fragment.room,
            text=fragment.content,
            metadata=meta,
        )

    async def get(self, user_id: str, key: str) -> MemoryWireRecord | None:
        did = self._doc_id(user_id, key)
        return self.docs.get(did)

    async def get_all(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MemoryWireRecord]:
        if not user_id.strip():
            items = sorted(
                self.docs.values(),
                key=lambda r: (
                    str(r.metadata.get("wing", r.user_id)),
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
            if r.metadata.get("user_id") == user_id
            or r.user_id == user_id
            or r.metadata.get("wing") == user_id
        ]
        sliced = filtered[offset or 0 :]
        if limit is not None:
            sliced = sliced[:limit]
        return sliced

    async def delete(self, user_id: str, key: str) -> None:
        did = self._doc_id(user_id, key)
        self.docs.pop(did, None)
