"""Recall ranking and public metadata exposure."""

from __future__ import annotations

import pytest

from eidolon.memory.adapters.recall_ranking import (
    rank_records_by_similarity,
    vector_fields_from_hit,
)
from eidolon.memory.adapters.search_payload import parse_search_tool_payload
from eidolon.memory.application.public_recall import wire_record_to_public_dict
from eidolon.memory.domain.wire import MemoryWireRecord


def test_vector_fields_prefers_similarity_over_distance():
    sim, internal = vector_fields_from_hit(
        {"distance": 0.9, "similarity": 0.42, "text": "x"}
    )
    assert sim == 0.42
    assert internal["_distance"] == 0.9


def test_vector_fields_derives_similarity_from_distance():
    sim, internal = vector_fields_from_hit({"distance": 0.6, "text": "铁锤"})
    assert sim == pytest.approx(0.4)
    assert internal["_distance"] == 0.6


def test_rank_records_by_similarity_descending():
    hits = [
        MemoryWireRecord(
            memory_space_id="Wing_Life",
            key="a",
            value="far",
            metadata={"similarity": 0.2},
        ),
        MemoryWireRecord(
            memory_space_id="Wing_Life",
            key="b",
            value="铁锤",
            metadata={"similarity": 0.8},
        ),
    ]
    out = rank_records_by_similarity(hits, top_k=2)
    assert [r.key for r in out] == ["b", "a"]


def test_parse_search_payload_exposes_similarity_not_score():
    rows = parse_search_tool_payload(
        {
            "results": [
                {
                    "text": "用户提到：铁锤是一只边境牧羊犬",
                    "wing": "Wing_Life",
                    "room": "event_general",
                    "distance": 0.6,
                    "similarity": 0.55,
                }
            ]
        }
    )
    assert rows[0].metadata["similarity"] == 0.55
    assert "score" not in rows[0].metadata
    assert rows[0].metadata["_distance"] == 0.6

    public = wire_record_to_public_dict(rows[0])
    assert public["metadata"]["similarity"] == 0.55
    assert "score" not in public["metadata"]
    assert "_distance" not in public["metadata"]
