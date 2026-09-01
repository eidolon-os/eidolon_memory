"""Shared semantic recall logic for MCP read tools and HTTP admin (same behavior)."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from eidolon_memory_contracts import MemoryActorContext

from eidolon.memory.adapters.recall_ranking import public_metadata, rank_records_by_similarity
from eidolon.memory.application.kg_recall import expand_from_recalled, query_kg_for_recall
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.application.recall_rerank import rerank_bm25_rrf
from eidolon.memory.application.scope_policy import interaction_readable_audiences
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import MemoryBackendUnavailable
from eidolon.memory.domain.ports import MemoryReader, ScopedMemoryReader
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.cpu_env import recommend_max_wing_parallel
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


def wire_record_to_public_dict(rec: MemoryWireRecord) -> dict[str, Any]:
    payload = rec.model_dump(mode="json")
    payload["metadata"] = public_metadata(rec.metadata)
    return payload


def recall_record_visible_for_context(
    rec: MemoryWireRecord,
    context: MemoryActorContext,
    *,
    include_private: bool = False,
) -> bool:
    return RecallPolicyRegistry.default().visible(
        rec,
        context=context,
        include_private=include_private,
    )


# Wings excluded from the default competitive vector fan-out.
#   Wing_Privacy — never recalled (privacy boundary).
#   Wing_Theme   — Phase 4.1: themes are a SEPARATE retrieval channel
#     (``_fetch_themes`` searches Wing_Theme on its own budget and renders
#     into the [主题] section). Letting Wing_Theme compete in the shared
#     top_k let broad theme summaries evict specific facts on
#     precision-sensitive queries (negative / future_plans / preference) —
#     measured at -15..-17pp in the consolidator A/B bench. Excluding it here
#     keeps the specific-fact top_k clean while themes still surface via
#     their own channel.
_FANOUT_EXCLUDED_WINGS = frozenset({"Wing_Privacy", "Wing_Theme"})


def _resolve_wings(
    settings: MemorySettings,
    *,
    wing: str | None,
) -> list[str]:
    if wing:
        return [wing]  # explicit single-wing request (incl. callers wanting Wing_Theme)
    return [w.id for w in settings.wings if w.id not in _FANOUT_EXCLUDED_WINGS]


def _effective_wing_parallel(settings: MemorySettings, *, for_voice: bool) -> int:
    if for_voice:
        return recommend_max_wing_parallel(settings, role="livekit")
    explicit = settings.runtime.read.max_wing_parallel
    if explicit > 0:
        return explicit
    # Half the cores, capped at 4: a non-voice recall should not saturate a box
    # that is also serving voice traffic.
    #
    # This read `min(4, os.cpu_count() or 4 // 2)` before, which parses as
    # `os.cpu_count() or 2` — `//` binds tighter than `or`. On any machine
    # reporting its cores that yielded the full count, capped at 4, so the
    # halving never happened.
    return max(1, min(4, (os.cpu_count() or 4) // 2))


async def recall_with_kg_fusion(
    backend: MemoryReader,
    settings: MemorySettings,
    *,
    query: str,
    context: MemoryActorContext,
    top_k: int,
    kg: object | None,
    for_voice: bool = False,
    palace_path: str | None = None,
    include_sensitive_kg: bool = False,
    kg_subjects: list[str] | None = None,
) -> dict[str, list]:
    """KG plan §5: parallel vector + KG via ``asyncio.gather``.

    Returns ``{"vector": [MemoryWireRecord], "kg": [KgTripleRecord]}``. The KG
    side honors :data:`RecallPolicy.kg_timeout_seconds` (default 0.05s);
    timeout silently degrades to vector-only — never raises into the caller so
    LiveKit's 300ms budget stays intact.
    """
    recall_kind = "voice" if for_voice else "chat"
    started = time.perf_counter()
    diagnostics: dict[str, float] = {}

    async def _vector_path() -> list[MemoryWireRecord]:
        vector_started = time.perf_counter()
        result = await search_all_wings_mcp_style(
            backend,
            settings,
            query=query,
            context=context,
            top_k=top_k,
            wing=None,
            room=None,
            for_voice=for_voice,
            palace_path=palace_path,
            raise_on_degraded=True,
            diagnostics=diagnostics,
        )
        diagnostics["vector_total_ms"] = _elapsed_ms(vector_started)
        return result

    vector_task = asyncio.create_task(_vector_path())

    kg_task: asyncio.Task | None = None
    if kg is not None and settings.recall.kg_in_recall:
        # Voice keeps the hard 50ms inside LiveKit's 300ms deadline; off that
        # path there is more room, but not unlimited — a chat reply is still
        # waiting. Both budgets are configurable; the non-voice one used to be a
        # hardcoded second, which is longer than the entire budget a chat
        # retrieval is supposed to fit in.
        #
        # A cold first call can still block the to_thread executor on ONNX work
        # for longer than this. Dropping the graph contribution is the right
        # outcome there: the vector result is already a usable answer, and the
        # warm path is what the budget is for.
        kg_timeout = (
            settings.recall.kg_timeout_seconds
            if for_voice
            else settings.recall.kg_timeout_seconds_normal
        )

        async def _kg_seed_path() -> list:
            kg_started = time.perf_counter()
            result = await _kg_path_with_timeout(
                kg,
                # Scoped to what this authenticated Companion/Council may see.
                audiences=interaction_readable_audiences(context),
                query=query,
                max_entities=settings.recall.kg_max_entities,
                max_triples_per_entity=settings.recall.kg_max_triples_per_entity,
                timeout_s=kg_timeout,
                include_sensitive=include_sensitive_kg,
                kind=recall_kind,
                subject_names=kg_subjects,
            )
            diagnostics["kg_seed_ms"] = _elapsed_ms(kg_started)
            return result

        kg_task = asyncio.create_task(_kg_seed_path())

    vector_degraded = False
    # Carried out with the result, not only logged. A caller that can only see
    # *that* recall degraded has to grep logs to learn whether the store was
    # unreachable, slow, or answered correctly with nothing — and those call for
    # different responses.
    degraded_reason: str | None = None
    try:
        vector_records = await vector_task
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        log.warning(
            "vector_recall_degraded",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        vector_records = []
        vector_degraded = True
        degraded_reason = f"{type(exc).__name__}: {exc}"
    kg_records = await kg_task if kg_task is not None else []

    # Phase 1 — BM25 + cosine RRF rerank on vector hits. Pure in-memory,
    # ~ms scale, fully bypassed when settings.recall.rerank_enabled = False.
    # Defensive fallback inside rerank_bm25_rrf returns hits[:top_k] on any
    # failure — never breaks recall.
    rank_started = time.perf_counter()
    if settings.recall.rerank_enabled and vector_records:
        vector_records = rerank_bm25_rrf(
            query,
            vector_records,
            top_k=top_k,
            rrf_k=settings.recall.rerank_rrf_k,
        )

    registry = RecallPolicyRegistry.default()
    vector_records = registry.rank(
        vector_records,
        context=context,
        query=query,
        top_k=max(top_k, len(vector_records)),
    )
    diagnostics["rerank_policy_ms"] = _elapsed_ms(rank_started)

    # The graph's second seed: one hop out from what was actually recalled.
    #
    # Here, after ranking, because the seed should be the memories this turn is
    # about — not the raw hit list, and not the phrase, which for "她住哪儿" names
    # nobody at all. Serialised behind the vector leg by necessity: it cannot
    # start until there are results to seed from. That is the cost; what it buys
    # is a seed that needs no matching, so it is a handful of indexed seeks.
    expand_started = time.perf_counter()
    kg_records = _merge_triples(
        kg_records,
        await _expand_with_timeout(
            kg,
            settings,
            records=vector_records,
            audiences=interaction_readable_audiences(context),
            include_sensitive=include_sensitive_kg,
            for_voice=for_voice,
            kind=recall_kind,
        ),
        # A statement from a turn already showing as a drawer is not new
        # information — the drawer text says it, in the person's own words. Drop
        # it and let the budget go to the hop.
        already_shown={
            str((record.metadata or {}).get("source_turn_id") or "") for record in vector_records
        },
        limit=settings.recall.kg_max_entities * settings.recall.kg_max_triples_per_entity,
    )
    diagnostics["kg_expand_ms"] = _elapsed_ms(expand_started)

    # Phase 4 — Wing_Theme drawers always surface (when present). They
    # encode cross-time "what's been on your mind" overviews that don't
    # compete on cosine ranking with concrete fragments; they're meant
    # to live in their own [主题] section, not displace vector hits.
    # We fetch them with a dedicated search and merge ADDITIVELY (no
    # truncation of vector_records). Dedup on key avoids double-counting
    # if a theme happened to win a vector top-K slot too.
    theme_started = time.perf_counter()
    theme_records = await _fetch_themes(backend, query, context, settings)
    if theme_records:
        existing_keys = {r.key for r in vector_records}
        # Themes go first in the merged list so the renderer's split-by-
        # `_is_theme_record` puts the [主题] section in front naturally.
        merged: list[MemoryWireRecord] = [r for r in theme_records if r.key not in existing_keys]
        merged.extend(vector_records)
        vector_records = merged
    diagnostics["theme_ms"] = _elapsed_ms(theme_started)

    _record_recall(
        kind=recall_kind,
        settings=settings,
        graph_enabled=kg is not None,
        degraded=vector_degraded,
        elapsed=time.perf_counter() - started,
        vector_count=len(vector_records),
        kg_count=len(kg_records),
    )
    diagnostics["recall_total_ms"] = _elapsed_ms(started)
    return {
        "vector": vector_records,
        "kg": kg_records,
        "degraded": vector_degraded,
        "degraded_reason": degraded_reason,
        "trace": diagnostics,
    }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _record_recall(
    *,
    kind: str,
    settings: MemorySettings,
    graph_enabled: bool,
    degraded: bool,
    elapsed: float,
    vector_count: int,
    kg_count: int,
) -> None:
    """Report one recall.

    ``outcome`` separates empty from degraded because they look identical to a
    caller and mean opposite things here: empty is a correct answer about a space
    with nothing relevant, degraded is us failing to look properly.
    """

    metrics.RECALL_SECONDS.labels(
        kind=kind,
        backend=settings.mempalace.backend,
        graph="on" if graph_enabled else "off",
        degraded="true" if degraded else "false",
    ).observe(elapsed)
    if degraded:
        outcome = "degraded"
    elif vector_count or kg_count:
        outcome = "hit"
    else:
        outcome = "empty"
    metrics.RECALL_TOTAL.labels(kind=kind, outcome=outcome).inc()


async def _fetch_themes(
    backend: MemoryReader,
    query: str,
    context: MemoryActorContext,
    settings: MemorySettings,
) -> list[MemoryWireRecord]:
    """Pull up to ``recall.theme_top_k`` Wing_Theme drawers.

    Uses ``backend.search`` against the Wing_Theme scope only — cheap
    (single wing, ≤ cap rows). Returns ``[]`` when no themes exist or
    when the configured cap is 0 (a deliberate disable).
    """
    cap = max(0, int(settings.recall.theme_top_k))
    if cap == 0:
        return []
    try:
        hits = await backend.search(
            query,
            wing="Wing_Theme",
            n_results=cap,
            room=None,
            audiences=interaction_readable_audiences(context),
        )
    except Exception as exc:  # noqa: BLE001 - never break recall
        log.warning("theme_fetch_failed", error=str(exc))
        return []

    # Phase 4.1 — relevance floor. Themes are broad cross-time summaries;
    # fetched unconditionally they leak onto out-of-scope queries (a pet
    # theme surfacing on "我家鸟会说话吗"). Drop themes whose cosine
    # similarity is below the configured floor. ``similarity`` is stamped by
    # ``parse_search_tool_payload``; absent (e.g. fakes) → keep the hit so
    # tests that don't model similarity still see themes.
    floor = float(settings.recall.theme_min_similarity)
    if floor <= 0.0:
        return hits
    kept: list[MemoryWireRecord] = []
    for r in hits:
        sim = (r.metadata or {}).get("similarity")
        if sim is None or float(sim) >= floor:
            kept.append(r)
    return kept


def _merge_triples(
    primary: list,
    expanded: list,
    *,
    already_shown: set[str],
    limit: int,
) -> list:
    """Combine the graph's two seeds into one bounded list.

    ``primary`` (the phrase and the caller's hint) keeps its order ahead of the
    expansion: those seeds came from what was asked, while the expansion comes
    from what was found. Which is more useful is not knowable here, and
    preferring the asked-about entity is the conservative reading.

    **A statement whose turn is already showing as a drawer sorts last, and is
    not dropped.** It is close to redundant — the drawer carries the person's own
    sentence for the same turn — so it should lose the budget to a fact the
    drawers did not carry. But dropping it outright silences the graph entirely
    on a young palace, where nearly every statement shares a turn with a recalled
    drawer, and that is the same failure this whole change exists to fix: the
    graph going quiet exactly where it is being asked to help. It also is not
    strictly redundant — a triple is resolved ("妈妈" → 张丽) and carries
    validity, which the raw fragment does not.
    """

    seen_ids: set[str] = set()
    novel: list = []
    restating: list = []
    for record in (*primary, *expanded):
        identity = getattr(record, "id", None)
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        turn = str(getattr(record, "source_turn_id", "") or "")
        (restating if turn and turn in already_shown else novel).append(record)
    return [*novel, *restating][:limit]


async def _expand_with_timeout(
    kg,
    settings: MemorySettings,
    *,
    records: list[MemoryWireRecord],
    audiences: tuple[str, ...],
    include_sensitive: bool,
    for_voice: bool,
    kind: str,
) -> list:
    """The expansion leg, on the same budget and the same silent degradation.

    Separate from ``_kg_path_with_timeout`` because it starts later and can
    therefore be skipped entirely — if the vector leg found nothing, there is
    nothing to expand from, and the phrase-seeded leg has already run.
    """

    if kg is None or not settings.recall.kg_in_recall or not records:
        return []
    turn_ids = list(
        dict.fromkeys(
            str((record.metadata or {}).get("source_turn_id") or "") for record in records
        )
    )
    turn_ids = [turn for turn in turn_ids if turn]
    if not turn_ids:
        return []

    timeout_s = (
        settings.recall.kg_timeout_seconds
        if for_voice
        else settings.recall.kg_timeout_seconds_normal
    )
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            expand_from_recalled(
                kg,
                audiences=audiences,
                source_turn_ids=turn_ids,
                max_entities=settings.recall.kg_max_entities,
                max_triples_per_entity=settings.recall.kg_max_triples_per_entity,
                include_sensitive=include_sensitive,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        metrics.GRAPH_TIMEOUTS.labels(kind=kind).inc()
        log.warning(
            "kg_expand_timeout",
            timeout_s=timeout_s,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            turn_count=len(turn_ids),
        )
        return []
    except Exception as exc:  # noqa: BLE001 - the graph is never allowed to break recall
        log.warning("kg_expand_failed", error=str(exc), error_type=type(exc).__name__)
        return []


async def _kg_path_with_timeout(
    kg,
    *,
    audiences: tuple[str, ...],
    query: str,
    max_entities: int,
    max_triples_per_entity: int,
    timeout_s: float,
    include_sensitive: bool,
    kind: str,
    subject_names: list[str] | None = None,
) -> list:
    """Route entity candidates → one combined SQL; degrade silently on timeout.

    ``kg.match_entities_for_query`` owns the naming-convention bridge
    (literal + prefix-strip), so this layer just asks "give me candidates"
    without knowing about ``pet:`` / ``place:`` / ``mother:`` prefixes.
    Future synonym / alias support extends that method, not this caller.
    """
    t0 = time.monotonic()
    try:

        async def _inner():
            # Both, always. The hint used to short-circuit the phrase entirely,
            # so a caller naming one entity discarded every other entity the
            # person had just mentioned. The hint leads — the caller knows
            # something this layer does not — and the phrase fills the rest of
            # the budget behind it.
            hinted = list(dict.fromkeys(subject_names or []))
            candidates = hinted[:max_entities]
            if len(candidates) < max_entities:
                from_phrase = await kg.match_entities_for_query(
                    query,
                    audiences=audiences,
                    cap=max_entities - len(candidates),
                )
                candidates.extend(name for name in from_phrase if name not in set(candidates))
            if not candidates:
                log.debug(
                    "kg_recall_result",
                    query_len=len(query or ""),
                    candidate_count=0,
                    triple_count=0,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
                return []
            triples = await query_kg_for_recall(
                kg,
                audiences=audiences,
                entity_names=candidates,
                max_triples_per_entity=max_triples_per_entity,
                include_sensitive=include_sensitive,
            )
            log.info(
                "kg_recall_result",
                query_len=len(query or ""),
                candidate_count=len(candidates),
                triple_count=len(triples),
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
            return triples

        return await asyncio.wait_for(_inner(), timeout=timeout_s)
    except TimeoutError:
        # ``kind`` is passed in, not inferred. It used to read
        # ``"voice" if timeout_s <= 0.1 else "chat"`` — deriving the caller's
        # intent from a number the caller had already decided, three lines after
        # the caller computed the answer as ``recall_kind``. Any change to the
        # voice budget silently relabelled the series, and a chat recall
        # configured below 100 ms would have been counted as voice.
        metrics.GRAPH_TIMEOUTS.labels(kind=kind).inc()
        log.warning(
            "kg_recall_timeout",
            timeout_s=timeout_s,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            query_len=len(query or ""),
        )
        return []
    except Exception as exc:
        log.warning("kg_recall_failed", error=str(exc))
        return []


async def search_all_wings_mcp_style(
    backend: MemoryReader,
    settings: MemorySettings,
    *,
    query: str,
    context: MemoryActorContext,
    top_k: int,
    wing: str | None,
    room: str | None,
    for_voice: bool = False,
    palace_path: str | None = None,
    raise_on_degraded: bool = False,
    diagnostics: dict[str, float] | None = None,
) -> list[MemoryWireRecord]:
    """Search configured wings in parallel, filter, rank, and cap top_k."""
    vector_started = time.perf_counter()
    scope_started = time.perf_counter()
    wings = _resolve_wings(settings, wing=wing)
    audiences = interaction_readable_audiences(context)
    if diagnostics is not None:
        diagnostics["scope_resolution_ms"] = _elapsed_ms(scope_started)
    vector_degraded = False
    use_shared_embedding = for_voice or settings.runtime.read.normal_shared_query_embedding
    scoped_reader = (
        backend
        if isinstance(backend, ScopedMemoryReader) and backend.supports_scoped_search
        else None
    )
    if (
        use_shared_embedding
        and wings
        and settings.runtime.read.shared_query_embedding
        and scoped_reader
    ):
        try:
            scoped_hits = await scoped_reader.search_scoped(
                query=query,
                wings=wings,
                room=room,
                audiences=audiences,
                n_results=top_k,
                skip_closets=(settings.runtime.read.voice_skip_closets if for_voice else False),
                diagnostics=diagnostics,
            )
            hits = [
                record
                for record in scoped_hits
                if recall_record_visible_for_context(record, context)
            ]
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if raise_on_degraded:
                raise MemoryBackendUnavailable(
                    f"{'voice' if for_voice else 'normal'} shared embedding search failed: {exc}"
                ) from exc
            # Chroma/mempalace can surface pyo3 panic wrappers as BaseException
            # rather than Exception. Recall is a degraded dependency, so return
            # no vector hits instead of taking the whole MCP worker/session down.
            log.warning(
                (
                    "voice_shared_embedding_search_failed"
                    if for_voice
                    else "normal_shared_embedding_search_failed"
                ),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            hits = []
            vector_degraded = True
    else:
        parallel = _effective_wing_parallel(settings, for_voice=for_voice)
        sem = asyncio.Semaphore(parallel)

        async def _one(wing_id: str) -> list[MemoryWireRecord]:
            async with sem:
                found = await backend.search(
                    query,
                    wing=wing_id,
                    n_results=top_k,
                    room=room,
                    audiences=audiences,
                )
                # memory_space_id is stamped at the source (backend.search →
                # parse_search_tool_payload with the palace's authoritative id).
                return [r for r in found if recall_record_visible_for_context(r, context)]

        batches = await asyncio.gather(*[_one(wid) for wid in wings], return_exceptions=True)
        hits = []
        for wing_id, batch in zip(wings, batches, strict=False):
            if isinstance(batch, BaseException):
                vector_degraded = True
                log.warning(
                    "vector_wing_search_failed",
                    memory_space_id=context.memory_space_id,
                    wing=wing_id,
                    query_len=len(query or ""),
                    error=str(batch),
                    error_type=type(batch).__name__,
                )
                continue
            hits.extend(batch)

    if vector_degraded and raise_on_degraded and not hits:
        raise MemoryBackendUnavailable("vector search degraded")

    hits = rank_records_by_similarity(hits, top_k=max(top_k, len(hits)))
    result = RecallPolicyRegistry.default().rank(
        hits,
        context=context,
        query=query,
        top_k=top_k,
    )
    if diagnostics is not None:
        diagnostics["vector_pipeline_ms"] = _elapsed_ms(vector_started)
    return result
