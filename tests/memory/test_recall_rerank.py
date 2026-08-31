"""Phase 1 — BM25 + cosine RRF rerank unit tests.

Pure module tests:no real backend, just feed `MemoryWireRecord` lists and
check ordering. The recall fusion integration is covered separately.
"""

from __future__ import annotations

from unittest.mock import patch

from eidolon.memory.application.recall_rerank import (
    DEFAULT_RRF_K,
    _rrf_fuse,
    _tokenize,
    rerank_bm25_rrf,
)
from eidolon.memory.domain.wire import MemoryWireRecord


def _rec(text: str, key: str | None = None) -> MemoryWireRecord:
    return MemoryWireRecord(
        memory_space_id="default.u1.default",
        key=key or f"k-{abs(hash(text)) % 100000}",
        value=text,
        metadata={"memory_type": "preference"},
    )


# ─── Tokenizer ─────────────────────────────────────────────────────────────


def test_tokenize_chinese_per_char():
    assert _tokenize("我喝乌龙茶") == ["我", "喝", "乌", "龙", "茶"]


def test_tokenize_english_per_word_lowercase():
    assert _tokenize("Hello World") == ["hello", "world"]


def test_tokenize_mixed_zh_en_digits():
    out = _tokenize("OP-3091 项目延期")
    assert "op-3091" in out
    assert "项" in out and "目" in out


def test_tokenize_handles_empty_and_whitespace():
    assert _tokenize("") == []
    assert _tokenize("   \t  ") == []


# ─── RRF math ──────────────────────────────────────────────────────────────


def test_rrf_unanimous_ranking_preserved():
    # Both rankings agree: result must agree too.
    fused = _rrf_fuse([[0, 1, 2], [0, 1, 2]], n=3, rrf_k=DEFAULT_RRF_K)
    assert fused == [0, 1, 2]


def test_rrf_unanimous_top_choice_kept_when_others_disagree():
    """If both rankers put the same doc at #0, RRF agrees regardless of下面 order."""
    fused = _rrf_fuse([[2, 0, 1], [2, 1, 0]], n=3, rrf_k=DEFAULT_RRF_K)
    assert fused[0] == 2


def test_rrf_full_inverse_keeps_extremes_top_middle_bottom():
    """k=60 是 soft fusion: 完全反序时,RRF 让两个排序的"高位"都进 top-2,
    "都被排到中间"的反而排末位。Cormack 2009 §3 实测一致。"""
    fused = _rrf_fuse([[0, 1, 2], [2, 1, 0]], n=3, rrf_k=DEFAULT_RRF_K)
    # doc 0(cosine #0) 和 doc 2(BM25 #0) tied 平局,各占 top-2;
    # doc 1(两个排序中都是 middle)反而最末
    assert set(fused[:2]) == {0, 2}, f"top-2 应含 cosine #0 + BM25 #0, got {fused}"
    assert fused[-1] == 1, f"middle-of-both 应在末位, got {fused}"


def test_rrf_tie_break_stable_by_index():
    # If both rankings put doc 0 and doc 1 in opposite positions:
    # ranking_a: [0, 1] / ranking_b: [1, 0] → equal RRF score → first-index wins.
    fused = _rrf_fuse([[0, 1], [1, 0]], n=2, rrf_k=DEFAULT_RRF_K)
    assert fused == [0, 1]


# ─── rerank_bm25_rrf orchestration ──────────────────────────────────────────


def test_rerank_empty_returns_empty():
    assert rerank_bm25_rrf("query", [], top_k=5) == []


def test_rerank_zero_top_k_returns_empty():
    hits = [_rec("foo"), _rec("bar")]
    assert rerank_bm25_rrf("foo", hits, top_k=0) == []


def test_rerank_single_hit_returns_it():
    hits = [_rec("only one")]
    out = rerank_bm25_rrf("anything", hits, top_k=5)
    assert len(out) == 1
    assert out[0].value == "only one"


def test_rerank_top_k_truncation():
    hits = [_rec(f"doc-{i}") for i in range(10)]
    out = rerank_bm25_rrf("doc-0 doc-1", hits, top_k=3)
    assert len(out) == 3


def test_rerank_lifts_lexical_match_above_cosine_noise():
    """k=60 soft fusion: BM25 lifts the lexically matching doc from cosine
    bottom toward the top, but cosine 仍主导信号——期望 lexical match 进 top-2
    且其位置严格优于原 cosine rank。"""
    hits = [
        _rec("用户最近在听 Acquired 播客"),  # cosine #1 (irrelevant to query)
        _rec("用户最近在思考人生"),  # cosine #2 (also irrelevant)
        _rec("用户喝乌龙茶不喝咖啡"),  # cosine #3 (DIRECTLY matches query)
    ]
    out = rerank_bm25_rrf("我喝什么茶", hits, top_k=3)
    lexical = "用户喝乌龙茶不喝咖啡"
    values = [r.value for r in out]
    # Lexical match must reach top-2 after fusion (RRF k=60 不会硬抢 cosine #1)
    assert lexical in values[:2], f"expected lexical match in top-2, got: {values}"
    # And it must have moved UP relative to cosine: original idx 2 → new idx < 2
    assert values.index(lexical) < 2, (
        f"BM25 should lift lexical match from cosine #3 toward top, got: {values}"
    )


def test_rerank_preserves_metadata():
    """Metadata travel verbatim through rerank — Phase 4 themes / Phase 5
    downstream ranking will read metadata fields."""
    hits = [
        MemoryWireRecord(
            memory_space_id="default.u1.default",
            key=f"k{i}",
            value=f"text {i}",
            metadata={"memory_type": "profile", "similarity": 0.9 - i * 0.1},
        )
        for i in range(3)
    ]
    out = rerank_bm25_rrf("text", hits, top_k=3)
    sim_values = sorted(r.metadata["similarity"] for r in out)
    assert sim_values == [0.7, 0.8, 0.9], "metadata dropped during rerank"


def test_rerank_deterministic_same_input_same_order():
    hits = [_rec(t) for t in ["alpha 茶", "beta 茶", "alpha", "gamma"]]
    out1 = rerank_bm25_rrf("茶 alpha", hits, top_k=4)
    out2 = rerank_bm25_rrf("茶 alpha", hits, top_k=4)
    assert [r.value for r in out1] == [r.value for r in out2]


def test_rerank_falls_back_to_cosine_when_bm25_unavailable(monkeypatch):
    """Defensive降级:模块加载时 rank-bm25 缺失,rerank 应等价于截断 cosine。"""
    monkeypatch.setattr("eidolon.memory.application.recall_rerank._BM25_AVAILABLE", False)
    hits = [_rec(f"doc-{i}") for i in range(5)]
    out = rerank_bm25_rrf("anything", hits, top_k=3)
    assert [r.value for r in out] == ["doc-0", "doc-1", "doc-2"]


def test_rerank_falls_back_when_bm25_raises():
    """BM25 抛错(例如全停用词 corpus)— 仍返回 cosine 前 N,不破坏 recall。"""
    hits = [_rec(t) for t in ["a", "b", "c"]]

    def _boom(*args, **kwargs):
        raise RuntimeError("bm25 exploded")

    with patch(
        "eidolon.memory.application.recall_rerank._BM25Okapi",
        side_effect=_boom,
    ):
        out = rerank_bm25_rrf("a b", hits, top_k=2)
    assert [r.value for r in out] == ["a", "b"]


def test_rerank_handles_empty_query_string():
    """Empty query → no BM25 signal,fallback to cosine order(don't blow up)。"""
    hits = [_rec(t) for t in ["x", "y", "z"]]
    out = rerank_bm25_rrf("", hits, top_k=3)
    assert [r.value for r in out] == ["x", "y", "z"]


def test_rerank_handles_corpus_all_empty_values():
    """Records with empty `value` → BM25 corpus is degenerate → cosine fallback."""
    hits = [
        MemoryWireRecord(memory_space_id="default.u1.default", key=f"k{i}", value="", metadata={})
        for i in range(3)
    ]
    out = rerank_bm25_rrf("query", hits, top_k=3)
    assert len(out) == 3
