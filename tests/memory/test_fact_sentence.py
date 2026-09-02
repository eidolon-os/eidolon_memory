"""One sentence per fact, and both paths get it from the same place.

The drawer a triple projects is the text that gets embedded, and the deployed
embedder is BGE-small-zh. It used to be built with a bare f-string —
``f"{subject} {predicate} {object}"`` — so a benchmark palace held drawers
reading ``self owns pet:铁锤`` and ``self partner_of wife``: 18 of 38, asked to
match Chinese questions like 我家狗多大. The read path had rendered the same
triple into Chinese all along, through a table the writer never called.

A fix on 2026-08-04 repaired the renderer and left the writer, which is the
half retrieval depends on. These tests pin the property that made that
possible: not "the table is correct" but "there is one table, and both paths
read it".
"""

from __future__ import annotations

import pytest
from eidolon_memory_contracts import KG_PREDICATE_VALUES

from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.domain.predicates import (
    _PREDICATE_TEMPLATES,
    fact_sentence,
    predicate_definitions,
)


def _record(subject: str, predicate: str, object_: str) -> KgTripleRecord:
    return KgTripleRecord(
        id="t-1",
        subject=subject,
        predicate=predicate,
        object=object_,
        valid_from="2026-01-01T00:00:00Z",
        confidence=0.9,
        audience="owner",
    )


@pytest.mark.parametrize(
    ("subject", "predicate", "object_"),
    [
        ("self", "owns", "pet:铁锤"),
        ("self", "partner_of", "wife"),
        ("mother", "has_state", "失眠"),
        ("pet:铁锤", "holds_role", "边境牧羊犬"),
        ("self", "holds_role", "实习生"),
        ("project:OP-3091", "has_state", "被甲方退回"),
    ],
)
def test_the_drawer_and_the_rendered_line_are_the_same_sentence(
    subject: str, predicate: str, object_: str
) -> None:
    """They diverged for a month and only the invisible half was checked."""

    from eidolon.memory.application.kg_recall import plain_triple_sentence

    assert fact_sentence(subject, predicate, object_) == plain_triple_sentence(
        _record(subject, predicate, object_)
    )


def test_no_schema_token_survives_into_a_fact_sentence() -> None:
    """``self``, and the ``pet:``/``project:`` prefixes, are storage shapes.

    Embedded verbatim they are tokens the person never said, so the drawer
    cannot be found by their words and the model reads them as part of a fact
    about them.
    """

    sentence = fact_sentence("self", "owns", "pet:铁锤")

    assert "self" not in sentence
    assert "pet:" not in sentence
    assert sentence == "用户 拥有 铁锤"


def test_every_wire_predicate_has_a_sentence_of_its_own() -> None:
    """The registry and the table cannot drift apart silently.

    A predicate with no template falls back to bare juxtaposition, which puts
    the English name back into the embedded text — the exact defect, reachable
    again by adding a predicate and forgetting the sentence.
    """

    registered = {definition.predicate for definition in predicate_definitions()}
    missing = sorted(p for p in registered if p not in _PREDICATE_TEMPLATES)

    assert not missing, f"predicates registered with no sentence template: {missing}"
    assert registered == set(KG_PREDICATE_VALUES) & registered


def test_the_projected_drawer_carries_the_sentence_not_the_triple() -> None:
    """Asserted on ``_drawer_for_triple`` itself, because that is the defect.

    The first version of this file compared ``fact_sentence`` with
    ``plain_triple_sentence`` — both of which go through the shared function —
    so reverting the writer to its bare f-string left every test green. That is
    the same vacuum the 2026-08-04 fix left behind: the renderer was checked and
    the writer, which produces the embedded text, was not. Sabotage-verified by
    putting the f-string back and watching this one fail.
    """

    from eidolon_memory_contracts import ConversationTurnPayload, build_memory_actor_context

    from eidolon.memory.application.turn_processor import _drawer_for_triple
    from eidolon.memory.domain.kg import KgTripleAction

    turn = ConversationTurnPayload(
        turn_id="t-1",
        context=build_memory_actor_context(
            owner_id="alice",
            companion_id="default",
            memory_realm_id="r:alice:default",
            device_id="device",
            session_id="s1",
        ),
        user_text="铁锤是我的边牧",
        assistant_text="记住了。",
        timestamp="2026-05-19T10:00:00Z",
    )
    drawer = _drawer_for_triple(
        KgTripleAction(subject="self", predicate="owns", object="pet:铁锤", confidence=0.9),
        turn=turn,
        turn_ts=turn.timestamp,
        assertion_id="a-1",
        evidence_id="e-1",
        projection_id="p-1",
    )

    assert drawer.content == "用户 拥有 铁锤"
    assert "self" not in drawer.content
    assert "owns" not in drawer.content


def test_an_unknown_predicate_still_renders_rather_than_vanishing() -> None:
    """Ugly beats dropped: a fact with no template is still a fact."""

    assert fact_sentence("self", "invented_by_a_model", "x") == "用户 invented_by_a_model x"
