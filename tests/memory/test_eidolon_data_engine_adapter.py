from __future__ import annotations

from eidolon_data.schema.types import MemoryItem, RecallOptions

from eidolon.memory.adapters.eidolon_data_engine import EidolonDataMemoryEngine
from eidolon.memory.domain.wire import MemoryWireRecord


class FakeBackend:
    lock = None
    working_memory = None

    def __init__(self) -> None:
        self.ingested = []

    async def ingest_text(self, *, wing: str, room: str, text: str, metadata=None) -> None:
        self.ingested.append((wing, room, text, metadata or {}))

    async def ingest_fragment(self, fragment) -> None:
        raise NotImplementedError

    async def search(self, query: str, *, wing: str, n_results: int = 5, room: str | None = None):
        assert query == "tea"
        assert wing == "realm-1"
        assert n_results == 3
        assert room == "preference"
        return [
            MemoryWireRecord(
                memory_space_id=wing,
                key="drawer-1",
                value="likes tea",
                metadata={"memory_id": "memory-1", "confidence": 0.9},
            )
        ]

    async def get(self, user_id: str, key: str):
        return None

    async def get_all(self, user_id: str, *, limit: int | None = None, offset: int | None = None):
        return []

    async def get_by_source_turn_id(self, memory_space_id: str, source_turn_id: str):
        return None

    async def delete(self, user_id: str, key: str) -> None:
        return None


async def test_eidolon_data_memory_engine_ingest_and_recall() -> None:
    backend = FakeBackend()
    engine = EidolonDataMemoryEngine(backend)

    result = await engine.ingest(
        MemoryItem(
            memory_id="memory-1",
            realm_id="realm-1",
            item_type="preference",
            content_summary="likes tea",
            payload={"value": "likes tea"},
        )
    )

    assert result.engine == "mempalace"
    assert result.memory_id == "memory-1"
    assert result.external_key == "realm-1/preference/memory-1"
    assert backend.ingested[0][0:3] == ("realm-1", "preference", "likes tea")

    result = await engine.recall(
        "realm-1",
        "tea",
        RecallOptions(top_k=3, filters={"room": "preference"}),
    )
    assert result.hits[0].memory_id == "memory-1"
    assert result.hits[0].score == 0.9
