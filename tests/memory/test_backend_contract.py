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
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    memory_space_id = "default.alice.default"
    settings = load_memory_settings().model_copy(deep=True)
    settings.mempalace.backend = "sqlite_exact"
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
        "eidolon.memory.adapters.mempalace_python_backend.probe_hnsw_safety",
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
        "eidolon.memory.adapters.mempalace_python_backend.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=False,
            status="unknown",
            message="probe unavailable",
        ),
    )

    def _search_memories(**kwargs):
        captured.update(kwargs)
        return {"results": []}

    monkeypatch.setattr("mempalace.searcher.search_memories", _search_memories)

    backend = MemPalacePythonBackend(load_memory_settings(), "/tmp/palace")
    assert backend.search_sync("hello", wing="Wing_Profile") == []
    assert captured["vector_disabled"] is False


def test_chroma_search_rehydrates_archived_privacy_before_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=False,
            status="ok",
            message="",
        ),
    )
    monkeypatch.setattr(
        "mempalace.searcher.search_memories",
        lambda **_kwargs: {
            "results": [
                {
                    "text": "我喜欢喝绿茶",
                    "wing": "Wing_Profile",
                    "room": "tea",
                    "similarity": 0.99,
                }
            ]
        },
    )

    class _RestrictedCollection:
        def get(self, *, ids, include):
            del include
            if ids == [_drawer_id("Wing_Profile", "tea", "我喜欢喝绿茶")]:
                return {
                    "ids": ids,
                    "metadatas": [
                        {
                            "wing": "Wing_Profile",
                            "room": "tea",
                            "privacy": "do_not_recall",
                            "memory_space_id": "default.alice.default",
                        }
                    ],
                }
            return {"ids": [], "metadatas": []}

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend._get_collection",
        lambda *_args, **_kwargs: _RestrictedCollection(),
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
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=False,
            status="ok",
            message="",
        ),
    )
    monkeypatch.setattr(
        "mempalace.searcher.search_memories",
        lambda **_kwargs: {
            "results": [
                {
                    "text": "我住在常州",
                    "wing": "Wing_Profile",
                    "room": "home",
                    "similarity": 0.99,
                }
            ]
        },
    )
    requested: list[str] = []

    class _HitCollection:
        def get(self, *, ids, include):
            del include
            requested.extend(ids)
            return {
                "ids": ids,
                "metadatas": [
                    {
                        "wing": "Wing_Profile",
                        "room": "home",
                        "privacy": "normal",
                        "memory_space_id": "default.alice.default",
                    }
                ],
            }

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend._get_collection",
        lambda *_args, **_kwargs: _HitCollection(),
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="default.alice.default",
    )

    hits = backend.search_sync("常州", wing="Wing_Profile")

    assert [hit.value for hit in hits] == ["我住在常州"]
    assert requested == [_drawer_id("Wing_Profile", "home", "我住在常州")]


def test_chroma_search_drops_hit_when_storage_metadata_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=False,
            status="ok",
            message="",
        ),
    )
    monkeypatch.setattr(
        "mempalace.searcher.search_memories",
        lambda **_kwargs: {
            "results": [
                {
                    "text": "无法验证来源的旧记录",
                    "wing": "Wing_Profile",
                    "room": "legacy",
                    "similarity": 0.99,
                }
            ]
        },
    )

    class _MissingCollection:
        def get(self, *, ids, include):
            del ids, include
            return {"ids": [], "metadatas": []}

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend._get_collection",
        lambda *_args, **_kwargs: _MissingCollection(),
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
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=False,
            status="ok",
            message="",
        ),
    )
    raw_text = '{"topic": "长期偏好", "items": ["茶", "散步"]}'
    monkeypatch.setattr(
        "mempalace.searcher.search_memories",
        lambda **_kwargs: {
            "results": [
                {
                    "text": raw_text,
                    "wing": "Wing_Theme",
                    "room": "theme",
                    "similarity": 0.99,
                }
            ]
        },
    )
    expected_id = _drawer_id("Wing_Theme", "theme", raw_text)

    class _JsonCollection:
        def get(self, *, ids, include):
            del include
            assert ids == [expected_id]
            return {
                "ids": ids,
                "metadatas": [
                    {
                        "wing": "Wing_Theme",
                        "room": "theme",
                        "privacy": "normal",
                        "memory_space_id": "default.alice.default",
                    }
                ],
            }

    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_python_backend._get_collection",
        lambda *_args, **_kwargs: _JsonCollection(),
    )
    backend = MemPalacePythonBackend(
        load_memory_settings(),
        "/tmp/palace",
        memory_space_id="default.alice.default",
    )

    hits = backend.search_sync("偏好", wing="Wing_Theme")

    assert hits[0].key == "theme"
    assert hits[0].value == {"topic": "长期偏好", "items": ["茶", "散步"]}
