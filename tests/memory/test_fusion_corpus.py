"""The fusion corpus, checked for the properties that make it worth running.

The corpus this replaces could not show a benefit from the graph, and the reason
was structural rather than a tuning problem: with eleven entities and almost every
edge hanging off ``self``, there was no path between two facts for a graph to
walk. Six of ten categories scored ``kg_hits=0`` in every run, and 21/49 sat
inside a ±1 band, so no change to retrieval could have been demonstrated either
way.

A corpus fixes that only if it really is connected, and "really" has to be
checked rather than intended. Two properties do the work, and both are easy to
break by editing one line of dialogue:

* A multi-hop question is only multi-hop while its two endpoints **never share a
  turn**. The moment one turn mentions both, a single vector hit answers it and
  the question stops discriminating — while still passing, which is the bad part.
* An invalidation question needs the superseded fact to actually be in the
  corpus, stated *earlier* than the fact that replaces it. Without the old
  statement present there is nothing to wrongly recall, and the case passes
  vacuously.

So these tests are about the fixture, not the code.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "e2e" / "fixtures"
CORPUS = FIXTURES / "fusion_corpus.jsonl"
QUERIES = FIXTURES / "fusion_queries.jsonl"


def _rows(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    return _rows(CORPUS)


@pytest.fixture(scope="module")
def queries() -> list[dict[str, Any]]:
    return _rows(QUERIES)


@pytest.fixture(scope="module")
def co_occurring(corpus: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Every unordered entity pair that shares at least one turn."""

    pairs: set[tuple[str, str]] = set()
    for turn in corpus:
        entities = sorted(set(turn.get("expected_entities") or []))
        pairs.update(itertools.combinations(entities, 2))
    return pairs


# ── the corpus is connected ───────────────────────────────────────────────────


def test_the_graph_is_not_a_star_around_self(
    corpus: list[dict[str, Any]], co_occurring: set[tuple[str, str]]
) -> None:
    """Edges between two non-self entities are what a second hop walks along.

    The previous corpus had six such turns out of forty, which is why no
    multi-hop question could be posed against it at all.
    """

    between_others = {(a, b) for a, b in co_occurring if a != "self" and b != "self"}

    assert len(between_others) >= 30, (
        f"only {len(between_others)} entity-to-entity pairs; a graph whose edges "
        f"all end at self has no path to walk"
    )


def test_there_are_enough_entities_to_tell_categories_apart(
    corpus: list[dict[str, Any]],
) -> None:
    counts = Counter(e for t in corpus for e in (t.get("expected_entities") or []))
    assert len(counts) >= 25, f"only {len(counts)} distinct entities: {sorted(counts)}"


def test_the_corpus_spans_months_and_is_ordered(corpus: list[dict[str, Any]]) -> None:
    """Time is the axis invalidation is measured along.

    The benchmark used to stamp every turn with ``now()``, which put the whole
    corpus inside one second. A corpus that carries timestamps is only useful
    while they are present, distinct enough to order, and actually increasing.
    """

    stamps = [t.get("timestamp") for t in corpus]
    assert all(stamps), "every turn needs a timestamp or time cannot be asked about"
    assert stamps == sorted(stamps), "turns must be in chronological order"
    assert stamps[0][:7] != stamps[-1][:7], "the corpus spans a single month"


# ── the multi-hop questions really are multi-hop ───────────────────────────────


def test_no_single_turn_answers_a_multi_hop_question(
    queries: list[dict[str, Any]], co_occurring: set[tuple[str, str]]
) -> None:
    """The property that makes these questions discriminate at all.

    If one turn mentions both endpoints, the vector path alone answers it and the
    case passes without the graph — indistinguishable, in the report, from the
    graph having worked.
    """

    hops = [q for q in queries if q["category"] == "multi_hop"]
    assert hops, "the fusion query set has no multi_hop cases"

    for query in hops:
        endpoints = query.get("hop_endpoints")
        assert endpoints and len(endpoints) == 2, (
            f"{query['id']}: a multi_hop case must declare its two endpoints"
        )
        pair = tuple(sorted(endpoints))
        assert pair not in co_occurring, (
            f"{query['id']}: {endpoints[0]} and {endpoints[1]} share a turn, so "
            f"one vector hit answers this and it no longer tests the graph"
        )


def test_every_multi_hop_question_has_a_bridge_that_reaches_both_ends(
    queries: list[dict[str, Any]], co_occurring: set[tuple[str, str]]
) -> None:
    """Unanswerable is as bad as trivially answerable.

    The bridge has to co-occur with each endpoint somewhere, or there is no
    two-hop path and the question is simply unanswerable — which would show up
    as the graph failing rather than as the corpus being wrong.
    """

    for query in [q for q in queries if q["category"] == "multi_hop"]:
        bridge = query.get("bridge")
        assert bridge, f"{query['id']}: no bridge entity declared"
        for endpoint in query["hop_endpoints"]:
            assert tuple(sorted((bridge, endpoint))) in co_occurring, (
                f"{query['id']}: {bridge} never shares a turn with {endpoint}, "
                f"so there is no path and the question cannot be answered"
            )


# ── the invalidation questions have something to get wrong ────────────────────


def _first_turn_saying(corpus: list[dict[str, Any]], terms: list[str]) -> int | None:
    """Index of the earliest turn whose text contains any of ``terms``."""

    for index, turn in enumerate(corpus):
        text = turn["user_text"] + turn["assistant_text"]
        if any(term in text for term in terms):
            return index
    return None


def test_the_old_fact_is_said_before_the_one_that_replaces_it(
    queries: list[dict[str, Any]], corpus: list[dict[str, Any]]
) -> None:
    """Checked on text, because text is what the scorer forbids.

    A first version of this asserted over entity ids, on the assumption that a
    superseded fact is always a distinct entity — true for 西湖区 → 滨江区 and
    false for everything else. A department, a job title and a delivery date have
    no entity of their own; what changes is an attribute, and what recall must
    not return is a *string*. Asserting over ids rejected three valid cases and
    would have accepted an invalid one whose forbidden term was never said.

    So: the forbidden term must appear in the corpus strictly before the term the
    answer is supposed to contain. Earlier is what makes it superseded; present is
    what makes the case non-vacuous.
    """

    invalidations = [q for q in queries if q["category"] == "invalidation"]
    assert invalidations, "the fusion query set has no invalidation cases"

    for query in invalidations:
        forbidden = query.get("forbidden_contains") or []
        expected = query.get("expected_vector_contains") or []
        assert forbidden, f"{query['id']}: nothing forbidden, so nothing is tested"
        assert expected, f"{query['id']}: no current fact to expect"

        old = _first_turn_saying(corpus, forbidden)
        new = _first_turn_saying(corpus, expected)
        assert old is not None, (
            f"{query['id']}: none of {forbidden} is ever said in the corpus, so "
            f"there is nothing for recall to wrongly return"
        )
        assert new is not None, (
            f"{query['id']}: none of {expected} is ever said, so the case cannot pass"
        )
        assert old < new, (
            f"{query['id']}: {corpus[old]['turn_id']} says the forbidden term and "
            f"{corpus[new]['turn_id']} says the expected one — the old fact must "
            f"come first or nothing was superseded"
        )


def test_the_forbidden_term_is_what_the_old_fact_reads_as(
    queries: list[dict[str, Any]], corpus: list[dict[str, Any]]
) -> None:
    """The scorer matches forbidden terms against text, not entity ids.

    A forbidden term nothing in the corpus actually says can never fire, so the
    guard would be decoration.
    """

    blob = " ".join(t["user_text"] + t["assistant_text"] for t in corpus)
    for query in [q for q in queries if q["category"] == "invalidation"]:
        forbidden = query.get("forbidden_contains") or []
        assert forbidden, f"{query['id']}: nothing forbidden, so nothing is tested"
        for term in forbidden:
            assert term in blob, (
                f"{query['id']}: {term!r} never appears in the corpus, so this guard cannot fire"
            )


# ── the labels line up with the corpus ────────────────────────────────────────


def test_every_query_names_at_least_one_entity_the_corpus_knows(
    queries: list[dict[str, Any]], corpus: list[dict[str, Any]]
) -> None:
    """Not every alternative — at least one, and that distinction is the point.

    The steward names entities itself, and the id it picks varies: a past run
    produced ``wife:王芳`` where the corpus labels say ``partner:王芳``. So
    ``expected_entities`` lists plausible variants, and most of them are
    deliberately not corpus labels. What would be a real defect is a query whose
    labels are *all* inventions — it can never hit the graph, and the report
    would show that as the graph having missed the question.
    """

    known = {e for t in corpus for e in (t.get("expected_entities") or [])}
    known |= {e.split(":", 1)[0] for e in known if ":" in e}
    known |= {e.split(":", 1)[1] for e in known if ":" in e}

    stranded = [
        q["id"]
        for q in queries
        if q.get("expected_entities") and not any(e in known for e in q["expected_entities"])
    ]
    assert not stranded, f"queries whose every entity label is invented: {stranded}"


def test_the_query_set_passes_the_benchmarks_own_validator(
    queries: list[dict[str, Any]],
) -> None:
    """The same check the bench runs before spending twenty minutes on a run."""

    from scripts.benchmark.bench_memory_retrieve_quality import _validate_queries

    _validate_queries(queries)


def test_abstention_cases_forbid_terms_the_corpus_would_otherwise_offer(
    queries: list[dict[str, Any]], corpus: list[dict[str, Any]]
) -> None:
    blob = " ".join(t["user_text"] + t["assistant_text"] for t in corpus)
    for query in [q for q in queries if q.get("negative") or q.get("expect_abstention")]:
        terms = query.get("forbidden_contains") or []
        assert terms, f"{query['id']}: an abstention case with nothing forbidden"
        assert any(term in blob for term in terms), (
            f"{query['id']}: none of {terms} appear in the corpus, so this case "
            f"cannot catch a plausible wrong answer"
        )
