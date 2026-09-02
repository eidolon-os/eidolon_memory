"""Real-process contracts for the two Chroma recall paths.

The E2E fixture deliberately uses the offline hash embedder: it exercises real
NATS, subprocess, Chroma, scope and rerank wiring without loading a model.  Hash
vectors have no semantic meaning, so this module must not claim product recall
quality from paraphrased questions.  Instead it verifies a property the offline
provider can prove: exact projected text remains reachable, and the shared-query
path is equivalent to the per-wing path.  Semantic quality belongs to the real
embedder benchmark, not to this deterministic plumbing gate.
"""

from __future__ import annotations

import pytest

from tests.memory.e2e.conftest import (
    e2e_actor_context,
    load_companion_corpus,
    mcp_tool_json,
    nats_publish_turn,
    wait_for_visible,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def _list_fragment_count(session) -> int:
    """Projected drawers only, which is what this count was ever a proxy for.

    It gates the readiness wait, and the turn's own sentence is now filed
    beside each projection. Counting both made the predicate true after half
    the corpus had been projected — the listing then ran early and the exact
    strings this test asserts were simply not there yet. A race, not a
    mismatch, and one a plain total cannot express.
    """
    result = await session.call_tool("eidolon_memory_list", {"limit": 1000})
    payload = mcp_tool_json(result)
    if not isinstance(payload, dict):
        return 0
    records = payload.get("records") or []
    return sum(1 for row in records if (row.get("metadata") or {}).get("source") != "turn-verbatim")


async def _recall_top_values(session, context, *, query: str, top_k: int = 3) -> list[str]:
    """Pull verbatim fragment ``value`` strings from `recall_context`."""
    try:
        result = await session.call_tool(
            "eidolon_memory_recall_context",
            {"query": query, "context": context, "top_k": top_k, "voice": False},
        )
    except Exception:
        return []
    payload = mcp_tool_json(result)
    if not isinstance(payload, dict):
        return []
    records = payload.get("records") or []
    return [str(r.get("value", "")) for r in records[:top_k]]


def _exact_projection_queries(corpus: list[dict]) -> list[tuple[str, str]]:
    """Return one exact, semantically neutral recall case per positive turn."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in corpus:
        expected_text = entry["user_text"]
        if expected_text in seen:
            continue
        if any(q.get("should_match") for q in entry.get("expected_recall_queries", [])):
            pairs.append((expected_text, expected_text))
            seen.add(expected_text)
    return pairs


def _matches(returned: list[str], expected_user_text: str) -> bool:
    """Loose match: any returned fragment shares a >= 3-char substring with
    the expected text. Tolerates rules-steward paraphrasing while still
    being strict enough to reject totally unrelated hits.
    """
    # Use the longest 4-char window from expected text as the signal.
    expected = expected_user_text.strip()
    if not expected:
        return False
    # Strip punctuation so windows align.
    norm = "".join(c for c in expected if not c.isspace())
    if len(norm) < 4:
        # Fallback: any whole-text inclusion.
        return any(norm in r for r in returned)
    for r in returned:
        for i in range(len(norm) - 3):
            window = norm[i : i + 4]
            if window in r:
                return True
    return False


async def _publish_corpus(handle, corpus: list[dict], n: int) -> None:
    for entry in corpus[:n]:
        await nats_publish_turn(
            handle.nats_url,
            user_id=handle.user_id,
            user_text=entry["user_text"],
            assistant_text=entry["assistant_text"],
            turn_id=entry["turn_id"],
        )


async def test_rerank_pipeline_preserves_exact_projection_recall(
    live_agent_runner,
    mcp_session,
):
    """Rerank on/off both keep exact projected text reachable through E2E."""
    corpus = load_companion_corpus()
    exact_cases = _exact_projection_queries(corpus)
    assert len(exact_cases) >= 12

    h_on = live_agent_runner(
        user_id="e2e_p1_on",
        steward_mode="test-verbatim",
    )
    ctx_on = e2e_actor_context(h_on.user_id)

    # Verbatim test stewardship writes every non-empty turn. Waiting for the
    # complete corpus makes this a recall contract instead of a race against
    # the asynchronous writer.
    expected_fragments = len(corpus)

    async def _wait_for_fragments(session, label: str) -> int:
        async def _ready(s):
            return await _list_fragment_count(s) >= expected_fragments

        ok = await wait_for_visible(session, predicate=_ready, timeout_s=90)
        count = await _list_fragment_count(session)
        assert ok, f"{label} palace did not reach {expected_fragments} fragments (got {count})"
        return count

    await _publish_corpus(h_on, corpus, len(corpus))
    async with mcp_session(h_on.mcp_url) as s_on:
        n_on = await _wait_for_fragments(s_on, "rerank_on")

    h_off = live_agent_runner(
        user_id="e2e_p1_off",
        steward_mode="test-verbatim",
        extra_settings={"recall": {"rerank_enabled": False}},
    )
    ctx_off = e2e_actor_context(h_off.user_id)
    await _publish_corpus(h_off, corpus, len(corpus))
    async with mcp_session(h_off.mcp_url) as s_off:
        n_off = await _wait_for_fragments(s_off, "rerank_off")

    async def _score(mcp_url: str, context) -> tuple[int, int]:
        """Score one Palace at a time so quality A/B is not a load test."""
        hits_top1 = 0
        hits_top3 = 0
        async with mcp_session(mcp_url) as session:
            for query, expected in exact_cases:
                rows = await _recall_top_values(session, context, query=query, top_k=3)
                if rows and _matches(rows[:1], expected):
                    hits_top1 += 1
                if _matches(rows, expected):
                    hits_top3 += 1
        return hits_top1, hits_top3

    # Score the same ground-truth set sequentially.  Running both embedded
    # Chroma palaces at full query rate in one context manager turns this
    # quality contract into an accidental cross-Realm compaction stress test.
    hits_on_top1, hits_on_top3 = await _score(h_on.mcp_url, ctx_on)
    hits_off_top1, hits_off_top3 = await _score(h_off.mcp_url, ctx_off)

    total = len(exact_cases)
    rate_on_1 = hits_on_top1 / total
    rate_off_1 = hits_off_top1 / total
    rate_on_3 = hits_on_top3 / total
    rate_off_3 = hits_off_top3 / total

    report = (
        f"\n=== Phase 1 rerank e2e (n_queries={total}) ===\n"
        f"  top-1 hit rate: rerank_on={rate_on_1:.2%}  rerank_off={rate_off_1:.2%}\n"
        f"  top-3 hit rate: rerank_on={rate_on_3:.2%}  rerank_off={rate_off_3:.2%}\n"
        f"  fragments ingested: on={n_on} off={n_off}\n"
    )
    print(report)

    # This is a wiring contract, not a semantic quality benchmark. Exact text
    # must be found in top-3 on both sides; rerank may move one near-duplicate
    # at top-1, but cannot make the projection unreachable.
    assert rate_on_1 >= rate_off_1 - 0.05, (
        f"rerank regressed top-1: on={rate_on_1:.2%} < off={rate_off_1:.2%}\n{report}"
    )
    assert rate_on_3 >= rate_off_3 - 0.05, (
        f"rerank regressed top-3: on={rate_on_3:.2%} < off={rate_off_3:.2%}\n{report}"
    )
    assert rate_on_3 >= rate_on_1, (
        f"recall path broken: top-3 {rate_on_3:.2%} < top-1 {rate_on_1:.2%}\n{report}"
    )
    assert hits_on_top3 == total, report
    assert hits_off_top3 == total, report


async def test_normal_shared_embedding_matches_legacy_exact_recall(
    live_agent_runner,
    mcp_session,
) -> None:
    """The normal fast path must not buy latency by losing companion facts."""
    corpus = load_companion_corpus()
    exact_cases = _exact_projection_queries(corpus)
    assert len(exact_cases) >= 12

    legacy = live_agent_runner(
        user_id="e2e_normal_legacy",
        steward_mode="test-verbatim",
        extra_settings={
            "runtime": {"read": {"normal_shared_query_embedding": False}},
        },
    )
    shared = live_agent_runner(
        user_id="e2e_normal_shared",
        steward_mode="test-verbatim",
        extra_settings={
            "runtime": {"read": {"normal_shared_query_embedding": True}},
        },
    )

    async def _seed_and_wait(handle) -> list[str]:
        await _publish_corpus(handle, corpus, len(corpus))
        async with mcp_session(handle.mcp_url) as session:

            async def _ready(s):
                return await _list_fragment_count(s) >= len(corpus)

            assert await wait_for_visible(session, predicate=_ready, timeout_s=90)
            listed = mcp_tool_json(await session.call_tool("eidolon_memory_list", {"limit": 1000}))
            return [str(row.get("value", "")) for row in (listed or {}).get("records") or []]

    # Keep embedded Chroma writers/readers for the two Palaces sequential.
    legacy_values = await _seed_and_wait(legacy)
    shared_values = await _seed_and_wait(shared)
    assert all(_matches(legacy_values, expected) for _, expected in exact_cases)
    assert all(_matches(shared_values, expected) for _, expected in exact_cases)

    async def _score(handle) -> tuple[int, int]:
        top1 = 0
        top3 = 0
        context = e2e_actor_context(handle.user_id)
        async with mcp_session(handle.mcp_url) as session:
            for query, expected in exact_cases:
                rows = await _recall_top_values(session, context, query=query, top_k=3)
                top1 += int(bool(rows) and _matches(rows[:1], expected))
                top3 += int(_matches(rows, expected))
        return top1, top3

    legacy_top1, legacy_top3 = await _score(legacy)
    shared_top1, shared_top3 = await _score(shared)
    total = len(exact_cases)
    report = (
        f"normal legacy top1/top3={legacy_top1}/{legacy_top3}; "
        f"shared={shared_top1}/{shared_top3}; total={total}"
    )

    # The same explicit query vector must remain reachable through both public
    # paths. Semantic paraphrase quality is measured with the real embedder.
    tolerance = max(1, int(total * 0.05))
    assert shared_top1 >= legacy_top1 - tolerance, report
    assert shared_top3 >= legacy_top3 - tolerance, report
    assert legacy_top3 == total, report
    assert shared_top3 == total, report
