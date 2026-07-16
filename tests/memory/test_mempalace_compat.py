from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon.memory.infrastructure.mempalace_compat import (
    collection_metric,
    distance_similarity,
    first_result_list,
)


def test_collection_metric_uses_supported_upstream_metric() -> None:
    collection = SimpleNamespace(distance_metric="l2")

    assert collection_metric(collection) == "l2"


@pytest.mark.parametrize(
    ("distance", "metric", "expected"),
    [(3.0, "l2", 0.25), (0.2, "cosine", 0.8)],
)
def test_distance_similarity_contract(
    distance: float,
    metric: str,
    expected: float,
) -> None:
    assert distance_similarity(distance, metric) == pytest.approx(expected)


def test_first_result_list_accepts_mapping_and_object_payloads() -> None:
    assert first_result_list({"documents": [["a", "b"]]}, "documents") == ["a", "b"]
    assert first_result_list(SimpleNamespace(documents=[["c"]]), "documents") == ["c"]
    assert first_result_list({}, "documents") == []
