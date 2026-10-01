"""Executable contracts for MemPalace 3.10.0 behavior Eidolon relies on."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest


def test_runtime_is_exactly_mempalace_3100() -> None:
    assert version("mempalace") == "3.10.0"


def test_collection_api_accepts_explicit_vectors_and_read_only() -> None:
    from inspect import signature

    from mempalace.backends.base import BaseCollection
    from mempalace.palace import get_collection

    assert "embeddings" in signature(BaseCollection.upsert).parameters
    assert "query_embeddings" in signature(BaseCollection.query).parameters
    assert "read_only" in signature(get_collection).parameters


def test_unknown_collection_is_rejected_before_storage_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mempalace.palace import CollectionNameMismatchError, get_collection

    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    palace = tmp_path / "palace"
    with pytest.raises(CollectionNameMismatchError, match="orphan_drawers"):
        get_collection(str(palace), collection_name="orphan_drawers", create=True)
    assert not palace.exists()


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
