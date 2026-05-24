"""Phase 1 — BM25 + cosine Reciprocal Rank Fusion (RRF).

Why
---
Pure cosine top-K is noisy:

- semantic neighborhoods overpower keyword exact matches
  (query "我喝什么茶" pulls "妈妈失眠" because both are日常 chatter)
- same wing produces 4-5 near-duplicate fragments that crowd out other wings
- low-confidence记录 vs high-confidence记录 排序不区分

BM25 + RRF adds a sparse signal (lexical overlap) and fuses with cosine:
each ranker contributes ``1/(rrf_k + rank)`` to the doc's final score, and
top-K is selected on the fused score. RRF is the *industry-standard* late-
fusion algorithm (Cormack et al. 2009);默认 ``rrf_k = 60`` 跟主流实现一致。

Performance
-----------
- BM25 is O(D × T) where D = docs, T = query tokens. Typical D < 50,
  T < 10 → < 1ms in Python on consumer hardware.
- RRF is O(D log D). Trivial.
- Total rerank overhead: ~5ms / recall on hot path. Voice 50ms budget intact.

Failure mode
------------
If ``rank_bm25`` is missing或 BM25 抛错 (空 corpus / 全停用词), 退化为恒等返回
(cosine-only)。**绝不打断 recall。**
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eidolon.memory.support.logging import get_logger

if TYPE_CHECKING:
    from eidolon.memory.domain.wire import MemoryWireRecord

log = get_logger(__name__)

# RRF standard k (Cormack et al. 2009). Smaller k = more aggressive ranking;
# larger k = softer fusion. 60 is the canonical default.
DEFAULT_RRF_K = 60

# Internal: cache the BM25 import status so we don't pay it per call.
try:
    from rank_bm25 import BM25Okapi as _BM25Okapi  # type: ignore[import-untyped]
    _BM25_AVAILABLE = True
except ImportError:  # pragma: no cover - tested via failure-injection test
    _BM25Okapi = None
    _BM25_AVAILABLE = False


def _tokenize(text: str) -> list[str]:
    """Cheap tokenizer:中文按字 + 英文按空白。

    Rationale: jieba 等中文分词器引入 megabyte 级模型 + 启动开销;字符切对
    BM25 这种 bag-of-words 模型够用(同字共现已足够拉相关性信号),且无任何
    依赖。英文按空白切以保留多词命中。
    """
    s = (text or "").strip()
    if not s:
        return []
    tokens: list[str] = []
    buf: list[str] = []

    def _flush_buf() -> None:
        if buf:
            tokens.append("".join(buf).lower())
            buf.clear()

    for ch in s:
        if ch.isspace():
            _flush_buf()
            continue
        # ASCII letters / digits cluster; everything else (CJK 标点等) split per char
        if ch.isascii() and (ch.isalnum() or ch in "_-"):
            buf.append(ch)
        else:
            _flush_buf()
            tokens.append(ch)
    _flush_buf()
    return [t for t in tokens if t]


def _record_text(rec: MemoryWireRecord) -> str:
    """Best-effort text representation for BM25 indexing."""
    return str(getattr(rec, "value", "") or "")


def _rrf_fuse(
    rankings: list[list[int]],
    *,
    n: int,
    rrf_k: int,
) -> list[int]:
    """Reciprocal Rank Fusion across multiple rankings.

    Each `ranking` is a list of doc indices in rank order (best first).
    Returns indices sorted by descending RRF score. Stable: ties break on
    first-ranking order (which is cosine in our usage).

    score(doc) = Σ_r 1 / (rrf_k + rank_r(doc))
    """
    scores: dict[int, float] = {i: 0.0 for i in range(n)}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            if 0 <= idx < n:
                scores[idx] += 1.0 / (rrf_k + rank)
    # Stable sort:scores desc, then preserve insertion-order tiebreaker.
    return sorted(range(n), key=lambda i: (-scores[i], i))


def rerank_bm25_rrf(
    query: str,
    hits: list[MemoryWireRecord],
    *,
    top_k: int,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[MemoryWireRecord]:
    """Fuse cosine ranking (hits 已按 cosine 排) with BM25 ranking via RRF.

    Args:
        query: 用户的查询字符串
        hits: cosine top-K 之后的 MemoryWireRecord 列表(顺序即 cosine rank)
        top_k: 返回前 N 条(融合后)
        rrf_k: RRF 平滑系数,默认 60

    Returns:
        Re-ordered hits,长度 ≤ ``top_k``。如 BM25 不可用或抛错,回退为
        ``hits[:top_k]``(等价于禁用 rerank)。
    """
    n = len(hits)
    if n == 0 or top_k <= 0:
        return []
    if n == 1:
        return list(hits[:top_k])

    cosine_ranking = list(range(n))  # hits 已经按 cosine 排好

    bm25_ranking: list[int] | None = None
    if _BM25_AVAILABLE:
        try:
            corpus_tokens = [_tokenize(_record_text(r)) for r in hits]
            # Skip BM25 if corpus is degenerate (e.g. all-empty docs)
            if any(corpus_tokens):
                bm25 = _BM25Okapi(corpus_tokens)
                q_tokens = _tokenize(query)
                if q_tokens:
                    scores = bm25.get_scores(q_tokens)
                    # Higher BM25 score = more relevant. Stable index order on ties.
                    bm25_ranking = sorted(
                        range(n), key=lambda i: (-float(scores[i]), i)
                    )
        except Exception as exc:  # noqa: BLE001 - defensive fallback
            log.warning("recall_rerank_bm25_failed", error=str(exc))
            bm25_ranking = None

    if bm25_ranking is None:
        # Degraded mode: cosine-only
        return list(hits[:top_k])

    fused_indices = _rrf_fuse(
        [cosine_ranking, bm25_ranking], n=n, rrf_k=rrf_k
    )
    return [hits[i] for i in fused_indices[:top_k]]
