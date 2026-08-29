"""Executable contracts for MemPalace 3.8.0 behavior Eidolon relies on."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest


def test_runtime_is_exactly_mempalace_380() -> None:
    assert version("mempalace") == "3.8.0"


def test_collection_api_accepts_explicit_vectors_and_read_only() -> None:
    from inspect import signature

    from mempalace.backends.base import BaseCollection
    from mempalace.palace import get_collection

    assert "embeddings" in signature(BaseCollection.upsert).parameters
    assert "query_embeddings" in signature(BaseCollection.query).parameters
    assert "read_only" in signature(get_collection).parameters


def test_empty_sqlite_file_is_not_detected_as_backend(tmp_path: Path) -> None:
    from mempalace.backends import detect_backends_for_path

    (tmp_path / "sqlite_exact.sqlite3").touch()

    assert detect_backends_for_path(str(tmp_path)) == []


def test_repair_preserves_knowledge_graph_sqlite_sidecars(tmp_path: Path) -> None:
    from mempalace.repair import _preserve_knowledge_graph_sqlite

    source = tmp_path / "source"
    dest = tmp_path / "dest"
    source.mkdir()
    expected: dict[str, bytes] = {}
    for suffix in ("", "-wal", "-shm"):
        name = f"knowledge_graph.sqlite3{suffix}"
        payload = f"payload:{suffix}".encode()
        expected[name] = payload
        (source / name).write_bytes(payload)

    copied = _preserve_knowledge_graph_sqlite(str(source), str(dest))

    assert set(copied) == set(expected)
    assert {path.name: path.read_bytes() for path in dest.iterdir()} == expected


def test_daemon_dedupe_is_active_only_not_exactly_once(tmp_path: Path) -> None:
    from mempalace.daemon import QueueStore

    store = QueueStore(tmp_path / "queue.sqlite3")
    first = store.enqueue("sync", {"n": 1}, dedupe_key="projection-1")
    duplicate_while_queued = store.enqueue("sync", {"n": 2}, dedupe_key="projection-1")
    assert duplicate_while_queued.id == first.id

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.id == first.id
    duplicate_while_running = store.enqueue("sync", {"n": 3}, dedupe_key="projection-1")
    assert duplicate_while_running.id == first.id

    store.finish(first.id, state="succeeded", only_if_running=True)
    after_terminal_state = store.enqueue("sync", {"n": 4}, dedupe_key="projection-1")
    assert after_terminal_state.id != first.id


def test_search_contract_exposes_safe_vector_fallback() -> None:
    from inspect import signature

    from mempalace.backends.chroma import hnsw_capacity_status
    from mempalace.searcher import search_memories

    assert callable(hnsw_capacity_status)
    params = signature(search_memories).parameters
    assert "vector_disabled" in params
    assert "source_file" in params


def test_embedding_threads_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mempalace.config import MempalaceConfig

    monkeypatch.setenv("MEMPALACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_THREADS", "3")
    assert MempalaceConfig().embedding_threads == 3
