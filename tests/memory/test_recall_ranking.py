"""Recall ranking and public metadata exposure."""

from __future__ import annotations

from eidolon.memory.adapters.mempalace_results import storage_record
from eidolon.memory.adapters.recall_ranking import (
    rank_records_by_similarity,
)
from eidolon.memory.application.public_recall import wire_record_to_public_dict
from eidolon.memory.domain.wire import MemoryWireRecord


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


def test_boost_changes_order_without_changing_public_similarity():
    boosted = storage_record(
        "drawer_a",
        "boosted",
        {
            "similarity": 0.4,
            "_distance": 0.6,
            "_retrieval_score": 0.9,
            "source_turn_id": "turn-a",
            "source_file": "note.md",
        },
    )
    direct = storage_record("drawer_b", "direct", {"similarity": 0.8})
    assert rank_records_by_similarity([direct, boosted], top_k=2) == [boosted, direct]
    public = wire_record_to_public_dict(boosted)
    assert public["metadata"]["similarity"] == 0.4
    assert public["metadata"]["source_turn_id"] == "turn-a"
    assert not any(k.startswith("_") for k in public["metadata"])


def test_equal_scores_preserve_input_order():
    rows = [storage_record(f"drawer_{i}", str(i), {"similarity": 0.5}) for i in range(3)]
    assert rank_records_by_similarity(rows, top_k=2) == rows[:2]
