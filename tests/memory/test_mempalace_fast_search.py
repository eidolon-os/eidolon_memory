from __future__ import annotations

import pytest

from eidolon.memory.adapters.mempalace_fast_search import _score_results


class _QueryResult:
    documents = [["hello"]]
    metadatas = [[{"wing": "Wing_Work", "room": "project_x", "source_file": "note.md"}]]
    distances = [[3.0]]


def test_score_results_uses_metric_aware_similarity_for_l2() -> None:
    rows = _score_results(
        _QueryResult(),
        wings=["Wing_Work"],
        room=None,
        n_results=1,
        closet_boost_by_source={},
        post_filter=False,
        metric="l2",
    )

    assert rows[0]["similarity"] == pytest.approx(0.25)

