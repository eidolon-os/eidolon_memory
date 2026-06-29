"""``group_recall_context`` (recall_renderer.py) — pure rendering tests.

These tests cover ONLY the rendering layer:given a list of MemoryWireRecord
+ optional KG triples, produce the LLM-facing ``[MEMORY]`` block string.
Fusion / wing fan-out / KG routing are tested elsewhere.
"""

from __future__ import annotations

from eidolon.memory.application.recall_renderer import (
    _DEFAULT_GROUP,
    _WING_GROUP_MAP,
    _classify,
    group_recall_context,
)
from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.domain.wire import MemoryWireRecord


def _rec(memory_type: str, value: str, memory_space_id: str = "u1") -> MemoryWireRecord:
    return MemoryWireRecord(
        memory_space_id=memory_space_id,
        key=f"k-{abs(hash((memory_type, value))) % 10000}",
        value=value,
        metadata={"memory_type": memory_type},
    )


def test_empty_inputs_render_empty_string():
    assert group_recall_context([]) == ""
    assert group_recall_context([], kg_triples=[]) == ""


def test_known_memory_type_routes_to_named_section():
    records = [_rec("profile", "用户喜欢早起"), _rec("health", "用户在跑步")]
    out = group_recall_context(records)
    assert "个人画像与健康:" in out
    assert "- 用户喜欢早起" in out
    assert "- 用户在跑步" in out


def test_unknown_memory_type_falls_back_to_lifestyle():
    records = [_rec("totally-bogus", "fallback content")]
    out = group_recall_context(records)
    assert _DEFAULT_GROUP + ":" in out
    assert "- fallback content" in out


def test_vector_memory_renders_canonical_date_when_available():
    records = [
        MemoryWireRecord(
            memory_space_id="u1",
            key="k-time",
            value="铁锤今天去洗澡",
            metadata={
                "memory_type": "life",
                "occurred_at": "2026-05-18T20:00:00Z",
            },
        )
    ]
    out = group_recall_context(records)
    assert "- [2026-05-18] 铁锤今天去洗澡" in out


def test_classify_covers_all_memory_types_in_map():
    """sanity: every value in _WING_GROUP_MAP routes to its title via _classify."""
    for title, members in _WING_GROUP_MAP.items():
        for m in members:
            assert _classify(m) == title


def test_classify_case_insensitive():
    assert _classify("PROFILE") == _classify("profile")
    assert _classify("Health") == _classify("health")


def test_per_section_cap_4_items():
    """Each section is capped at 4 items; extras are silently dropped."""
    records = [_rec("life", f"item-{i}") for i in range(10)]
    out = group_recall_context(records)
    # 10 input → 4 output max in 生活方式与近况
    items = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(items) == 4, f"expected 4 capped items, got {len(items)}: {items}"


def test_section_order_stable_in_output():
    """Sections appear in CANONICAL order regardless of input order."""
    records = [
        _rec("life", "life-A"),       # 生活方式与近况
        _rec("emotion", "emo-A"),     # 情绪
        _rec("profile", "prof-A"),    # 个人画像与健康
    ]
    out = group_recall_context(records)
    # 个人画像 first, then 情绪, then 生活方式 (per _WING_GROUP_MAP key order)
    p1 = out.find("个人画像与健康:")
    p2 = out.find("情绪:")
    p3 = out.find("生活方式与近况:")
    assert 0 <= p1 < p2 < p3, f"section ordering broken: {(p1, p2, p3)}\nout=\n{out}"


def test_kg_triples_section_appears_before_vector():
    records = [_rec("profile", "用户喜欢早起")]
    triple = KgTripleRecord(
        id="t1", subject="self", predicate="likes",
        object="跑步", valid_from="2026-01-01T00:00:00Z", valid_to=None,
    )
    out = group_recall_context(records, kg_triples=[triple])
    assert "知识图谱事实" in out
    assert out.find("知识图谱事实") < out.find("个人画像与健康:")


def test_kg_only_no_vector_renders_only_kg_section():
    triple = KgTripleRecord(
        id="t1", subject="self", predicate="likes",
        object="茶", valid_from=None, valid_to=None,
    )
    out = group_recall_context([], kg_triples=[triple])
    assert out.startswith("知识图谱事实")
    # No vector groups in output
    assert "个人画像与健康:" not in out


def test_empty_kg_triples_list_treated_same_as_none():
    records = [_rec("life", "x")]
    assert group_recall_context(records, kg_triples=[]) == group_recall_context(records)
