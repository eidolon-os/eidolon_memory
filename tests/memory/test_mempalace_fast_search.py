from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon.memory.adapters.mempalace_fast_search import (
    _combined_where,
    _device_visibility_filter,
    _score_results,
    search_memories_shared_embedding,
)


class _QueryResult:
    documents = [["hello"]]
    metadatas = [[{"wing": "Wing_Work", "room": "project_x", "source_file": "note.md"}]]
    distances = [[3.0]]


def test_score_results_uses_metric_aware_similarity_for_l2() -> None:
    rows = _score_results(
        _QueryResult(),
        wings=["Wing_Work"],
        room=None,
        audiences=None,
        n_results=1,
        closet_boost_by_source={},
        post_filter=False,
        metric="l2",
    )

    assert rows[0]["similarity"] == pytest.approx(0.25)


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

    rows = search_memories_shared_embedding(
        "hello",
        "/tmp/palace",
        wings=["Wing_Profile", "Wing_Work"],
        room=None,
        n_results=2,
        query_embedding=[0.1, 0.2],
    )

    assert [row["wing"] for row in rows] == ["Wing_Profile", "Wing_Work"]
    assert len(calls) == 2
    assert all(call["vector_disabled"] is True for call in calls)
