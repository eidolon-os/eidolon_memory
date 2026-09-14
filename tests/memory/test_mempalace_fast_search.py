from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon.memory.adapters.mempalace_fast_search import (
    _combined_where,
    _device_visibility_filter,
    _score_results,
    search_memories_shared_embedding,
)


def test_score_results_uses_metric_aware_similarity_for_l2() -> None:
    from mempalace.backends.base import QueryResult

    rows = _score_results(
        QueryResult(
            ids=[["drawer_real"]],
            documents=[["hello"]],
            metadatas=[[{"wing": "Wing_Work", "room": "project_x"}]],
            distances=[[3.0]],
        ),
        n_results=1,
        closet_boost_by_source={},
        metric="l2",
    )

    assert rows[0].metadata["similarity"] == pytest.approx(0.25)


def test_a_caller_without_a_device_excludes_device_scoped_rows_in_the_query() -> None:
    assert _device_visibility_filter(None) == {"visibility": {"$ne": "current_device"}}
    assert _device_visibility_filter("") == {"visibility": {"$ne": "current_device"}}


def test_a_caller_with_a_device_keeps_only_its_device_scoped_rows() -> None:
    assert _device_visibility_filter("device-a") == {
        "$or": [
            {"visibility": {"$ne": "current_device"}},
            {"source_device_id": "device-a"},
            {"target_device_id": "device-a"},
        ]
    }


def test_the_combined_where_clause_carries_the_device_predicate() -> None:
    clause = _combined_where(["Wing_Profile"], None, ("owner",), "device-a")

    assert clause is not None
    flattened = repr(clause)
    assert "source_device_id" in flattened
    assert "current_device" in flattened
    assert "current_device" in repr(_combined_where([], None, ("owner",), None))


def test_diverged_hnsw_uses_sqlite_fallback_without_opening_vector_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.probe_hnsw_safety",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_disabled=True,
            status="diverged",
            message="test divergence",
        ),
    )

    def _must_not_open_collection(*_args, **_kwargs):
        raise AssertionError("voice fallback touched the unsafe vector collection")

    monkeypatch.setattr("mempalace.palace.get_collection", _must_not_open_collection)
    calls: list[dict[str, object]] = []

    def _search_memories(**kwargs):
        calls.append(kwargs)
        return {
            "results": [
                {
                    "text": f"fallback-{kwargs['wing']}",
                    "wing": kwargs["wing"],
                    "room": "profile",
                    "similarity": None,
                    "bm25_score": 1.0,
                }
            ]
        }

    monkeypatch.setattr("mempalace.searcher.search_memories", _search_memories)

    from eidolon.memory.domain.errors import MemoryBackendUnavailable

    with pytest.raises(MemoryBackendUnavailable, match="visibility metadata"):
        search_memories_shared_embedding(
            "hello",
            "/tmp/palace",
            wings=["Wing_Profile", "Wing_Work"],
            room=None,
            n_results=2,
            query_embedding=[0.1, 0.2],
        )
    assert calls[0]["vector_disabled"] is True


@pytest.mark.parametrize("field", ["ids", "documents", "metadatas", "distances"])
def test_misaligned_storage_results_fail_visibly(field):
    from mempalace.backends.base import QueryResult

    from eidolon.memory.domain.errors import MemoryBackendUnavailable

    fields = dict(ids=[["drawer_a"]], documents=[["a"]], metadatas=[[{}]], distances=[[0.1]])
    fields[field] = [[]]
    with pytest.raises(MemoryBackendUnavailable, match="misaligned"):
        _score_results(
            QueryResult(**fields),
            n_results=5,
            closet_boost_by_source={},
        )


def test_closet_boost_preserves_original_similarity_and_actual_ids():
    from mempalace.backends.base import QueryResult

    result = QueryResult(
        ids=[["actual_a", "actual_b"]],
        documents=[["a", "b"]],
        metadatas=[[{"source_file": "a.md"}, {"source_file": "b.md"}]],
        distances=[[0.2, 0.5]],
    )
    rows = _score_results(
        result,
        n_results=2,
        closet_boost_by_source={"b.md": (0, 0.2, "b")},
    )
    assert [r.metadata["_storage_id"] for r in rows] == ["actual_b", "actual_a"]
    assert rows[0].metadata["similarity"] == 0.5
    assert rows[0].metadata["_retrieval_score"] == pytest.approx(0.9)


def test_zero_results_does_no_io(monkeypatch):
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.probe_hnsw_safety",
        lambda *a: pytest.fail("zero results must not touch storage"),
    )
    assert search_memories_shared_embedding("q", "/unused", wings=[], room=None, n_results=0) == []


def test_multiple_wings_embed_once_and_voice_skips_closets(monkeypatch):
    from mempalace.backends.base import QueryResult

    embeddings = []
    opens = []
    queries = []

    def embed(query):
        embeddings.append(query)
        return [0.1, 0.2]

    def open_collection(*args, **kwargs):
        opens.append(kwargs)
        return SimpleNamespace(distance_metric="cosine", query=query)

    def query(**kwargs):
        queries.append(kwargs)
        return QueryResult.empty()

    monkeypatch.setattr("eidolon.memory.adapters.mempalace_fast_search.embed_query_vector", embed)
    monkeypatch.setattr("mempalace.palace.get_collection", open_collection)
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.probe_hnsw_safety",
        lambda *a: SimpleNamespace(vector_disabled=False),
    )
    assert (
        search_memories_shared_embedding(
            "茶",
            "/unused",
            wings=["Wing_Life", "Wing_Work"],
            room=None,
            n_results=3,
            skip_closets=True,
        )
        == []
    )
    assert embeddings == ["茶"]
    assert len(opens) == len(queries) == 1
    assert opens[0]["read_only"] is True
    assert queries[0]["query_embeddings"] == [[0.1, 0.2]]
    assert queries[0]["where"] == {"wing": {"$in": ["Wing_Life", "Wing_Work"]}}


def test_unsupported_filter_is_not_retried_without_scope(monkeypatch):
    from mempalace.backends.base import UnsupportedFilterError

    from eidolon.memory.domain.errors import MemoryBackendUnavailable

    queries = []

    def query(**kwargs):
        queries.append(kwargs)
        raise UnsupportedFilterError("unsupported filter")

    monkeypatch.setattr(
        "mempalace.palace.get_collection",
        lambda *a, **kw: SimpleNamespace(distance_metric="cosine", query=query),
    )
    monkeypatch.setattr(
        "eidolon.memory.adapters.mempalace_fast_search.probe_hnsw_safety",
        lambda *a: SimpleNamespace(vector_disabled=False),
    )
    with pytest.raises(MemoryBackendUnavailable, match="unsupported filter"):
        search_memories_shared_embedding(
            "茶",
            "/unused",
            wings=["Wing_Life"],
            room=None,
            n_results=3,
            query_embedding=[0.1, 0.2],
            audiences=("owner",),
        )
    assert len(queries) == 1
    assert "audience" in str(queries[0]["where"])
