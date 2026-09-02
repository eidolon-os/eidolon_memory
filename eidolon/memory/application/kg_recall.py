"""KG side of the hybrid recall path (KG plan §5).

One role:

* **transcription** — turn ``KgTripleRecord`` rows into one-line natural-language
  context entries so the LLM consuming the recall context can treat KG facts
  identically to drawer fragments.

Entity routing(自然语言 query → canonical entity names)lives on the KG
facade as :meth:`KnowledgeGraphPort.match_entities_for_query`, because
the naming convention(``pet:`` / ``place:`` / ``mother:`` 前缀)is KG's
internal knowledge — the recall router shouldn't need to know about it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eidolon.memory.domain.kg import KgTripleRecord
from eidolon.memory.domain.predicates import fact_sentence


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def query_kg_for_recall(
    kg,
    *,
    audiences: tuple[str, ...],
    entity_names: list[str],
    subject_names: list[str] | None = None,
    now_iso: str | None = None,
    max_triples_per_entity: int,
    include_sensitive: bool = False,
) -> list[KgTripleRecord]:
    """Statements relevant to a turn, bounded per entity and scoped to audience.

    ``audiences`` is required rather than defaulted. A default would be either
    too narrow (the owner layer only, quietly losing what this companion was
    told) or too wide (everything, showing one companion what another was told) —
    and the wide mistake is invisible until there is a second companion.

    **The seeds are a union, not a choice.** This read ``if subject_names: …``
    and returned, so a caller passing ``focus_subjects`` silently discarded every
    entity found in the phrase. A hint is meant to sharpen retrieval, and the
    contract says so; overriding it is not sharpening.

    Both directions for every seed, via ``query_entity_combined``. A caller
    naming an entity wants what is known about it, and half of that is incoming —
    "用户 的母亲是 张丽" is about 张丽 whichever end she is on. ``query_subjects``
    is the narrower outgoing-only read and is no longer on this path.
    """

    seeds = list(dict.fromkeys([*(subject_names or []), *entity_names]))
    if not seeds:
        return []
    return await kg.query_entity_combined(
        seeds,
        audiences=audiences,
        as_of=now_iso,
        include_sensitive=include_sensitive,
        limit_per_entity=max_triples_per_entity,
    )


async def expand_from_recalled(
    kg,
    *,
    audiences: tuple[str, ...],
    source_turn_ids: list[str],
    now_iso: str | None = None,
    max_entities: int,
    max_triples_per_entity: int,
    include_sensitive: bool = False,
) -> list[KgTripleRecord]:
    """One hop out from the memories vector search just returned.

    The seed that works when the phrase names nobody. "她住哪儿" and "我上次说的
    那个事" contain no entity, so phrase matching finds nothing and the graph stays
    silent — in exactly the turns where it has the most to add, since a phrase that
    *does* name someone is one the vector store was going to answer anyway.

    Vector search has already decided which memories this turn is about. Those
    drawers carry ``source_turn_id``, the statements are indexed by it, and the
    join is exact — no matching step, so nothing to be wrong about.

    **One hop, and the hop is the point.** Zero hops would return the statements
    those same turns produced, which the recalled drawer text mostly already says.
    The value is the second step: vector finds "妈妈说她腰不好", the graph adds that
    妈妈 is 张丽, lives in 杭州, works at a hospital — things the vector store did
    not return and was not asked for.

    Deeper is deliberately not attempted. Graphiti allows three hops, on cloud
    Neo4j with no per-turn deadline; one hop here has not been measured against a
    voice budget on the board yet, and widening before measuring is how a recall
    path acquires a tail.
    """

    if not source_turn_ids or max_entities <= 0:
        return []
    entities = await kg.entities_for_source_turns(source_turn_ids, cap=max_entities)
    if not entities:
        return []
    return await kg.query_entity_combined(
        entities,
        audiences=audiences,
        as_of=now_iso,
        include_sensitive=include_sensitive,
        limit_per_entity=max_triples_per_entity,
    )


#: What a triple is marked as, so the reader knows it is not something the user said.
INFERRED_MARK = "（推测）"


def _when(value: str | None) -> str:
    """A validity timestamp as a reader should see it.

    ``canonical_temporal`` widens a date to midnight on write, so a stored
    ``T00:00:00Z`` means "this was a date" and the time carries nothing — showing
    it spends attention on precision the statement never had. A time that is not
    midnight was actually said ("答应明天下午三点前"), and dropping it would lose
    the deadline, so it survives to the minute.
    """

    text = (value or "").strip()
    if not text:
        return ""
    if len(text) < 10:
        return text
    day, _, rest = text.partition("T")
    if not rest or rest.startswith("00:00:00"):
        return day
    return f"{day} {rest[:5]}"


def _heard_at(t: KgTripleRecord) -> str:
    """When the conversation this came from happened, to the month.

    ``recorded_at`` and deliberately not ``valid_from``: a fact can start years
    before anyone mentions it — "我 2015 年搬到杭州" said last week — and
    provenance is about how stale the *knowledge* is, not the fact.

    To the month because a day is precision this does not have. The steward reads
    a date out of conversation; presenting it to the day would invite the model to
    reason about an exactness that was never there.
    """

    stamp = (t.recorded_at or "").strip()
    return stamp[:7] if len(stamp) >= 7 else ""


def plain_triple_sentence(t: KgTripleRecord) -> str:
    """The bare sentence, with no inference mark and no qualifiers.

    What a person would have to say to be talking about this statement. Forgetting
    matches against it: the decorations exist for the model reading a prompt, and
    "（推测）" or "（根据 2026-03 的对话）" appearing in the text would stop any
    literal phrase from ever matching.
    """

    return fact_sentence(t.subject, t.predicate, t.object)


def transcribe_triple(t: KgTripleRecord) -> str:
    """Render one triple as a single readable Chinese line.

    This text goes into the ``[MEMORY]`` block and from there into the model's
    prompt, so a bad rendering is not cosmetic — it is a sentence the model reads
    as a fact about the user.

    **Marked as inferred, and dated, and not labelled by where it is stored.**

    Three things are being balanced and it is worth writing down which won:

    * A caller must not be able to tell whether this deployment keeps a graph.
      That is why the old ``知识图谱事实：`` header and ``[KG]`` prefix are gone —
      they named an implementation, in text the model reads.
    * A triple is *derived*, written at confidence 0.6 and above, while a vector
      fragment is something the person actually said. Rendering them
      indistinguishably would let a guess be read as a quote, so the mark stays.
    * How old the knowledge is changes how much it should be trusted, so the
      month it was heard travels with it.

    ``（推测）`` and ``（根据 2026-03 的对话）`` say both without saying "graph".
    """

    valid_from = _when(t.valid_from)
    valid_to = _when(t.valid_to)
    body = plain_triple_sentence(t)

    # Validity first, provenance second, in one bracket. They answer different
    # questions — "until when is this true" against "when did we hear it" — and a
    # deadline or an ended interval is product-meaningful in a way the storage
    # label never was, so those survive verbatim.
    parts: list[str] = []
    if t.predicate == "promised" and valid_to:
        parts.append(f"截至 {valid_to}")
    elif valid_to:
        parts.append(f"{valid_from or '？'} → {valid_to}，已结束")
    elif valid_from:
        parts.append(f"自 {valid_from}")

    # Provenance is added only when it says something the validity did not. A
    # fact that started and was heard in the same month gets one clause, not two
    # saying 2026-04 twice; a 2015 move mentioned last week gets both, because
    # that gap is exactly the thing worth showing.
    heard = _heard_at(t)
    if heard and heard != valid_from[:7]:
        parts.append(f"根据 {heard} 的对话")
    qualifier = f"（{'；'.join(parts)}）" if parts else ""
    return f"{INFERRED_MARK}{body}{qualifier}"


def transcribe_triples(triples: list[KgTripleRecord]) -> str:
    """The lines, with no heading above them.

    The heading used to be ``知识图谱事实：``. It was doing two jobs and both were
    wrong: it announced the storage to the model, and it made the graph a *block*
    — so a graph timeout removed a whole visible category from the prompt rather
    than making the memory slightly thinner. Each line now carries its own mark
    and stands on its own.
    """

    if not triples:
        return ""
    return "\n".join(f"- {transcribe_triple(t)}" for t in triples)
