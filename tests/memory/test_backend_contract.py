"""Backend contract behavior for the MemPalace Python adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon.memory.adapters.mempalace_python_backend import (
    MemPalacePythonBackend,
    _drawer_id,
    _metadata_for_chroma,
)
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon.memory.domain.errors import MemoryBackendUnsupported, MemoryBackendWriteFailed


def test_drawer_id_matches_mempalace_deterministic_shape():
    did = _drawer_id("Wing_Work", "project_x", "hello")
    assert did.startswith("drawer_Wing_Work_project_x_")
    assert len(did.rsplit("_", 1)[-1]) == 24


def test_metadata_for_chroma_serializes_nested_values():
    meta = _metadata_for_chroma({"tags": ["a", "b"], "importance": 4, "empty": None})
    assert meta["tags"] == '["a", "b"]'
    assert meta["importance"] == 4
    assert "empty" not in meta


def test_scoped_search_is_owned_by_mempalace_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    captured: dict[str, object] = {}

    def _scoped(query: str, palace_path: str, **kwargs):
        captured.update({"query": query, "palace_path": palace_path, **kwargs})
        return [
            {
                "text": "likes green tea",
                "wing": "Wing_Profile",
                "room": "preference",
                "similarity": 0.9,
                "metadata": {
                    "wing": "Wing_Profile",
                    "room": "preference",
                    "privacy": "normal",
                },
            }
        ]

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.search_memories_shared_embedding",
        _scoped,
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="realm-test",
    )

    hits = backend.search_scoped_sync(
        "green tea",
        wings=["Wing_Profile", "Wing_Work"],
        n_results=3,
        skip_closets=True,
    )

    assert captured["palace_path"] == "/tmp/palace"
    assert captured["wings"] == ["Wing_Profile", "Wing_Work"]
    assert captured["skip_closets"] is True
    assert hits[0].memory_space_id == "realm-test"
    assert hits[0].metadata["privacy"] == "normal"


def test_offline_warm_uses_the_same_explicit_vector_read_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_memory_settings()
    settings.mempalace.offline_embedding = True
    backend = MemPalacePythonBackend(settings, "/tmp/palace")
    seen: dict[str, object] = {}

    def _search(query: str, **kwargs):
        seen.update({"query": query, **kwargs})
        return []

    monkeypatch.setattr(backend, "search_scoped_sync", _search)
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.active_embedder",
        lambda: pytest.fail("offline warm must not initialize a model"),
    )

    backend._warm_read_path_sync(("Wing_Life", "Wing_Work"))

    assert seen == {
        "query": "warmup",
        "wings": ["Wing_Life", "Wing_Work"],
        "n_results": 1,
        "skip_closets": True,
    }


@pytest.mark.asyncio
async def test_delete_requires_drawer_id_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    backend = MemPalacePythonBackend(load_memory_settings(), "/tmp/palace")
    with pytest.raises(MemoryBackendUnsupported):
        await backend.delete("u", "not-a-drawer-id")


@pytest.mark.asyncio
async def test_privacy_batch_archive_and_delete_are_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")
    memory_space_id = "default.alice.default"
    settings = load_memory_settings().model_copy(deep=True)
    settings.mempalace.backend = "chroma"
    # What is under test is the privacy semantics of archive/delete, not recall
    # ranking, so skip the real embedder.
    settings.mempalace.offline_embedding = True
    backend = MemPalacePythonBackend(
        settings,
        str(tmp_path / "palace"),
        memory_space_id=memory_space_id,
    )
    await backend.ingest_text(
        wing="Wing_Profile",
        room="tea",
        text="likes tea",
        metadata={"memory_space_id": memory_space_id, "privacy": "normal"},
    )
    drawer_id = _drawer_id("Wing_Profile", "tea", "likes tea")

    with pytest.raises(MemoryBackendWriteFailed, match="another memory space"):
        await backend.delete_many("default.bob.default", [drawer_id])
    assert await backend.get(memory_space_id, drawer_id) is not None

    archived = await backend.archive_many(memory_space_id, [drawer_id])
    assert archived == [drawer_id]
    record = await backend.get(memory_space_id, drawer_id)
    assert record is not None
    assert record.metadata["privacy"] == "do_not_recall"

    deleted = await backend.delete_many(memory_space_id, [drawer_id])
    assert deleted == [drawer_id]
    assert await backend.get(memory_space_id, drawer_id) is None


def test_search_sync_disables_vector_when_hnsw_diverged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=True,
            status="diverged",
            message="test divergence",
        ),
    )

    def _search_memories(**kwargs):
        captured.update(kwargs)
        return {"results": []}

    monkeypatch.setattr("mempalace.searcher.search_memories", _search_memories)

    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="realm-test",
    )
    assert backend.search_sync("hello", wing="Wing_Profile") == []
    assert captured["vector_disabled"] is True


def test_search_sync_keeps_vector_for_inconclusive_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=False,
            status="unknown",
            message="probe unavailable",
        ),
    )

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.embed_query_vector",
        lambda _query: [1.0, 0.0],
    )

    def _query(_collection, **kwargs):
        from mempalace.backends.base import QueryResult

        captured.update(kwargs)
        return QueryResult.empty()

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search._query_collection", _query
    )
    monkeypatch.setattr(
        "mempalace.palace.get_collection",
        lambda *_args, **_kwargs: SimpleNamespace(distance_metric="cosine"),
    )

    backend = MemPalacePythonBackend(load_memory_settings(), "/tmp/palace")
    assert backend.search_sync("hello", wing="Wing_Profile") == []
    assert captured["query_embeddings"] == [[1.0, 0.0]]


def test_chroma_search_rehydrates_archived_privacy_before_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.search_memories_shared_embedding",
        lambda *_args, **_kwargs: [
                {
                    "text": "我喜欢喝绿茶",
                    "wing": "Wing_Profile",
                    "room": "tea",
                    "similarity": 0.99,
                    "metadata": {
                        "wing": "Wing_Profile",
                        "room": "tea",
                        "privacy": "do_not_recall",
                        "memory_space_id": "default.alice.default",
                        "_storage_metadata_verified": True,
                    },
                }
            ],
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="default.alice.default",
    )

    assert backend.search_sync("绿茶", wing="Wing_Profile") == []


def test_chroma_search_metadata_hydration_is_bounded_to_current_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    calls = 0

    def _search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [
            {
                "text": "我住在常州",
                "wing": "Wing_Profile",
                "room": "home",
                "similarity": 0.99,
                "metadata": {
                    "wing": "Wing_Profile",
                    "room": "home",
                    "privacy": "normal",
                    "memory_space_id": "default.alice.default",
                    "_storage_metadata_verified": True,
                },
            }
        ]

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.search_memories_shared_embedding",
        _search,
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="default.alice.default",
    )

    hits = backend.search_sync("常州", wing="Wing_Profile")

    assert [hit.value for hit in hits] == ["我住在常州"]
    assert calls == 1


def test_chroma_search_drops_hit_when_storage_metadata_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.search_memories_shared_embedding",
        lambda *_args, **_kwargs: [
                {
                    "text": "无法验证来源的旧记录",
                    "wing": "Wing_Profile",
                    "room": "legacy",
                    "similarity": 0.99,
                    "metadata": {"_storage_metadata_verified": False},
                }
            ],
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="default.alice.default",
    )

    assert backend.search_sync("旧记录", wing="Wing_Profile") == []


def test_chroma_search_reconstructs_json_drawer_id_from_exact_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    raw_text = '{"topic": "长期偏好", "items": ["茶", "散步"]}'
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.search_memories_shared_embedding",
        lambda *_args, **_kwargs: [
                {
                    "text": raw_text,
                    "wing": "Wing_Theme",
                    "room": "theme",
                    "similarity": 0.99,
                    "metadata": {
                        "wing": "Wing_Theme",
                        "room": "theme",
                        "privacy": "normal",
                        "memory_space_id": "default.alice.default",
                        "_storage_metadata_verified": True,
                    },
                }
            ],
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="default.alice.default",
    )

    hits = backend.search_sync("偏好", wing="Wing_Theme")

    assert hits[0].key == "theme"
    assert hits[0].value == {"topic": "长期偏好", "items": ["茶", "散步"]}


def test_the_vector_port_is_the_same_surface_under_either_name() -> None:
    """VectorStorePort names what a vector store must do; MemoryBackend names
    where it sits. A replacement matches the former."""

    from eidolon.memory.domain.ports import MemoryBackend, VectorStorePort

    assert VectorStorePort is MemoryBackend


def test_both_the_real_and_fake_backend_satisfy_the_port() -> None:
    """Structural, not inherited — a substitute need not subclass anything."""

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.domain.ports import VectorStorePort

    assert isinstance(FakeMemoryBackend(), VectorStorePort)
    assert isinstance(
        MemPalacePythonBackend(load_memory_settings(), "/tmp/palace"),
        VectorStorePort,
    )


async def test_recall_needs_no_more_than_the_hot_path_fields() -> None:
    """A backend populating only these fields must still serve conversation.

    This is what keeps the vector store swappable: recall depending on a sixth
    field should be a deliberate widening of the contract, not something a single
    call site introduces quietly.
    """

    from eidolon_memory_contracts import MemoryActorContext

    from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
    from eidolon.memory.application.public_recall import recall_with_kg_fusion
    from eidolon.memory.domain.ports import RECALL_HOT_PATH_FIELDS

    space = "default.alice.default"
    backend = FakeMemoryBackend()
    # Metadata carries exactly the hot-path fields and the tenant key the
    # visibility gate needs — nothing a particular store would add.
    await backend.ingest_text(
        wing="Wing_Life",
        room="colour",
        text="likes the colour green",
        metadata={
            "memory_space_id": space,
            "wing": "Wing_Life",
            "room": "colour",
            "source_file": "eidolon",
            "similarity": 0.9,
        },
    )

    fused = await recall_with_kg_fusion(
        backend,
        load_memory_settings(),
        query="colour",
        context=MemoryActorContext(
            memory_realm_id=space, owner_id="alice", companion_id="default"
        ),
        top_k=5,
        kg=None,
        for_voice=False,
        palace_path=None,
    )

    assert [record.value for record in fused["vector"]] == ["likes the colour green"]
    assert RECALL_HOT_PATH_FIELDS == {"text", "wing", "room", "source_file", "similarity"}
