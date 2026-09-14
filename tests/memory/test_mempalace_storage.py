"""Required real MemPalace/Chroma contracts; isolated deterministic vectors, no model server."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import MemoryBackendUnavailable, MemoryBackendWriteFailed


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    monkeypatch.setenv("MEMPALACE_HOME", str(tmp_path / "home"))
    settings = MemorySettings.model_validate(
        {
            "runtime": {
                "palaces_root": str(tmp_path / "palaces"),
                "run_dir": str(tmp_path / "run"),
            },
            "mempalace": {"offline_embedding": True},
            "kg": {"backend": "none"},
        }
    )
    router = LocalPalaceRouter(settings)
    try:
        yield await router.resolve("storage-contract")
    finally:
        await router.aclose()


async def test_empty_real_store_and_typed_result_identity(runtime):
    from mempalace.backends.base import GetResult, QueryResult
    from mempalace.palace import get_collection

    backend = runtime.backend
    assert await backend.get_all(runtime.space_id) == []
    assert await backend.search("anything", wing="Wing_Life") == []
    for i, text in enumerate(['{"茶": "乌龙"}', "喜欢散步"]):
        await backend.ingest_text(
            wing="Wing_Life",
            room="general",
            text=text,
            metadata={
                "memory_space_id": runtime.space_id,
                "source_turn_id": f"turn-{i}",
                "source_file": f"folder-{i}/same.md",
            },
        )
    collection = get_collection(runtime.palace_path, create=False, read_only=True)
    result = collection.get(include=["documents", "metadatas", "embeddings"])
    assert isinstance(result, GetResult)
    query = collection.query(query_embeddings=[list(result.embeddings[0])], n_results=2)
    assert isinstance(query, QueryResult)
    hits = await backend.search('{"茶": "乌龙"}', wing="Wing_Life", n_results=2)
    scoped = await backend.search_scoped('{"茶": "乌龙"}', wings=["Wing_Life"], n_results=2)
    assert hits == scoped
    assert {r.metadata["_storage_id"] for r in hits} == set(result.ids)
    assert {r.metadata["source_file"] for r in hits} == {"folder-0/same.md", "folder-1/same.md"}
    assert all(r.key == "general" for r in hits)
    assert len(await backend.get_many(runtime.space_id, [*result.ids, *result.ids, "missing"])) == 2
    assert await backend.get(runtime.space_id, "missing") is None
    assert len(await backend.get_all(runtime.space_id, limit=1, offset=1)) == 1
    assert await backend.get_by_source_turn_id("other-space", "turn-0") is None


async def test_real_filters_keep_audience_device_and_wing(runtime):
    backend = runtime.backend
    rows = [
        ("shared", "Wing_Life", "owner", "all_devices", "phone"),
        ("phone-only", "Wing_Life", "owner", "current_device", "phone"),
        ("laptop-only", "Wing_Life", "owner", "current_device", "laptop"),
        ("other-audience", "Wing_Life", "companion:other", "all_devices", "phone"),
        ("other-wing", "Wing_Work", "owner", "all_devices", "phone"),
    ]
    for text, wing, audience, visibility, device in rows:
        await backend.ingest_text(
            wing=wing,
            room="general",
            text=text,
            metadata={
                "memory_space_id": runtime.space_id,
                "audience": audience,
                "visibility": visibility,
                "source_device_id": device,
            },
        )

    async def visible(device):
        hits = await backend.search(
            "shared",
            wing="Wing_Life",
            room="general",
            n_results=10,
            audiences=("owner",),
            device_id=device,
        )
        return {r.value for r in hits}

    assert await visible(None) == {"shared"}
    assert await visible("phone") == {"shared", "phone-only"}
    assert await visible("laptop") == {"shared", "laptop-only"}


async def test_real_invalid_query_and_dimensions_are_errors(runtime):
    from mempalace.palace import get_collection

    await runtime.backend.ingest_text(
        wing="Wing_Life",
        room="general",
        text="hello",
        metadata={"memory_space_id": runtime.space_id},
    )
    collection = get_collection(runtime.palace_path, create=False, read_only=True)
    from mempalace.backends.base import UnsupportedFilterError

    with pytest.raises(UnsupportedFilterError):
        collection.get(where={"wing": {"$unknown": "Wing_Life"}})
    # Wrong explicit vector width must fail at the adapter boundary, never return [].
    from eidolon.memory.adapters.mempalace_fast_search import search_memories_shared_embedding

    with pytest.raises(MemoryBackendUnavailable, match="dimension"):
        search_memories_shared_embedding(
            "hello",
            runtime.palace_path,
            wings=["Wing_Life"],
            room=None,
            n_results=1,
            query_embedding=[1.0, 0.0],
        )


@pytest.mark.parametrize("room", ["../escape", "bad:name", "", "x" * 300])
async def test_invalid_names_cannot_bypass_upstream_validation(runtime, room):
    with pytest.raises(MemoryBackendWriteFailed):
        await runtime.backend.ingest_text(wing="Wing_Life", room=room, text="no write")
    assert await runtime.backend.get_all("") == []


async def test_graph_statistics_include_general(runtime, tmp_path):
    for wing in ("Wing_Life", "Wing_Work"):
        await runtime.backend.ingest_text(
            wing=wing, room="general", text=wing, metadata={"memory_space_id": runtime.space_id}
        )
    graph = await runtime.backend.room_graph()
    assert "general" in graph.rooms
    assert graph.stats["total_rooms"] == 1
    assert graph.stats["total_room_instances"] == 2
    assert graph.stats["explicit_tunnels"] == 0
