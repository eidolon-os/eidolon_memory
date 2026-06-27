from __future__ import annotations

from eidolon_data import DataSettings
from eidolon_data.schema.types import RecallOptions

from eidolon.memory.application.eidolon_data_runtime import (
    build_eidolon_data_memory_engine,
    open_eidolon_data_store,
)
from eidolon.memory.domain.wire import MemoryWireRecord


class FakeBackend:
    lock = None
    working_memory = None

    def __init__(self) -> None:
        self.ingested: list[tuple[str, str, str, dict]] = []

    async def ingest_text(self, *, wing: str, room: str, text: str, metadata=None) -> None:
        self.ingested.append((wing, room, text, metadata or {}))

    async def ingest_fragment(self, fragment) -> None:
        raise NotImplementedError

    async def search(self, query: str, *, wing: str, n_results: int = 5, room: str | None = None):
        assert query == "tea"
        assert wing == "realm-1"
        assert n_results == 8
        assert room == "preference"
        return [
            MemoryWireRecord(
                memory_space_id=wing,
                key="drawer-1",
                value="likes tea",
                metadata={"memory_id": "memory-1", "score": 0.97},
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


async def test_open_eidolon_data_store_wires_memory_engine(tmp_path) -> None:
    backend = FakeBackend()
    store = open_eidolon_data_store(
        data_settings=DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")),
        backend=backend,
        locked=False,
    )
    await store.init_schema()
    try:
        await store.owners.create(owner_id="owner-1")
        await store.companions.create(companion_id="companion-1", owner_id="owner-1")
        await store.memory_repo.create_realm(
            realm_id="realm-1",
            owner_id="owner-1",
            companion_id="companion-1",
        )

        ingest_result = await store.memory.ingest_item(
            memory_id="memory-1",
            realm_id="realm-1",
            item_type="preference",
            content_summary="likes tea",
            payload_json={"value": "likes tea"},
        )
        assert ingest_result.memory_id == "memory-1"
        assert ingest_result.engine == "mempalace"
        assert ingest_result.external_key == "realm-1/preference/memory-1"
        assert backend.ingested[0][0:3] == ("realm-1", "preference", "likes tea")

        result = await store.memory.recall_context(
            realm_id="realm-1",
            query="tea",
            options=RecallOptions(filters={"room": "preference"}),
        )
        assert result.hits[0].memory_id == "memory-1"
        assert result.hits[0].score == 0.97
    finally:
        await store.close()


def test_build_eidolon_data_memory_engine_requires_location_without_backend() -> None:
    try:
        build_eidolon_data_memory_engine(locked=False)
    except ValueError as exc:
        assert "memory_space_id or palace_path" in str(exc)
    else:
        raise AssertionError("expected ValueError")
