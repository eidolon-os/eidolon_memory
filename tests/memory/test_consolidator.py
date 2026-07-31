"""Phase 4 — unit tests for the consolidator worker logic.

Scope:
  * ``Theme.idempotency_hash`` is deterministic / input-sensitive.
  * ``group_drawers_by_wing`` honors the time window + wing allowlist.
  * ``_extract_themes_from_llm_response`` survives JSON wobbles (fences,
    extra prose, partial JSON).
  * ``_ingest_theme`` writes a Wing_Theme drawer with the expected schema.
  * Renderer's [主题] section logic (split + cap + label).

LLM-coupled paths (``synthesize_themes_for_wing`` / ``consolidate_once``
end-to-end) are exercised by the e2e test, not here — those tests gate
on a live LLM endpoint and live in ``tests/memory/e2e/``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from eidolon_memory_contracts import ConsolidatorIngestThemeCommand, MemoryActorContext
from nats.errors import NoRespondersError

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.adapters.locked_backend import LockedBackend
from eidolon.memory.application.recall_renderer import group_recall_context
from eidolon.memory.application.turn_processor import _ingest_theme
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.entrypoints.consolidator import (
    Theme,
    _extract_themes_from_llm_response,
    _list_all_drawers,
    group_drawers_by_wing,
)
from eidolon.memory.infrastructure.nats.query import NatsMemoryQueryClient

# ─── Theme.idempotency_hash ────────────────────────────────────────────────


def test_idempotency_hash_stable_for_same_input():
    """Same user/wing/window/drawer-set → same hash, every time."""
    t1 = Theme(text="x", underlying_wing="Wing_Work", confidence=0.8,
               source_drawer_ids=["d1", "d2", "d3"])
    t2 = Theme(text="DIFFERENT TEXT", underlying_wing="Wing_Work", confidence=0.5,
               source_drawer_ids=["d2", "d3", "d1"])  # order-independent
    h1 = t1.idempotency_hash(memory_space_id="default.alice.mochi", window_days=30)
    h2 = t2.idempotency_hash(memory_space_id="default.alice.mochi", window_days=30)
    assert h1 == h2, "hash must depend only on input drawer set, not theme content"


def test_idempotency_hash_changes_on_new_drawer():
    """Adding/removing a single drawer flips the hash."""
    base = Theme(text="x", underlying_wing="Wing_Work", confidence=0.8,
                 source_drawer_ids=["d1", "d2"])
    extended = Theme(text="x", underlying_wing="Wing_Work", confidence=0.8,
                     source_drawer_ids=["d1", "d2", "d3"])
    h1 = base.idempotency_hash(memory_space_id="default.alice.mochi", window_days=30)
    h2 = extended.idempotency_hash(memory_space_id="default.alice.mochi", window_days=30)
    assert h1 != h2


def test_idempotency_hash_isolates_user_and_window():
    """Same drawers under different (user, window) → different hashes."""
    t = Theme(text="x", underlying_wing="W", confidence=0.8, source_drawer_ids=["d1"])
    assert (
        t.idempotency_hash(memory_space_id="default.alice.mochi", window_days=30)
        != t.idempotency_hash(memory_space_id="default.bob.mochi", window_days=30)
    )
    assert (
        t.idempotency_hash(memory_space_id="default.alice.mochi", window_days=30)
        != t.idempotency_hash(memory_space_id="default.alice.mochi", window_days=7)
    )


# ─── group_drawers_by_wing ────────────────────────────────────────────────


def _drawer(*, wing: str, age_days: float, value: str = "x") -> dict:
    """Build a ``eidolon_memory_list``-shaped record dict."""
    created = datetime.now(UTC) - timedelta(days=age_days)
    return {
        "memory_space_id": "default.alice.mochi",
        "key": f"k-{wing}-{age_days}",
        "value": value,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": {"wing": wing, "memory_type": "preference"},
    }


def test_group_drawers_filters_window():
    """Drawers older than ``window_days`` are dropped."""
    drawers = [
        _drawer(wing="Wing_Work", age_days=5),
        _drawer(wing="Wing_Work", age_days=10),
        _drawer(wing="Wing_Work", age_days=45),   # outside 30d window
    ]
    grouped = group_drawers_by_wing(drawers, window_days=30)
    assert len(grouped["Wing_Work"]) == 2


def test_group_drawers_filters_to_themable_wings():
    """Wing_Theme + Wing_Privacy never reach the consolidator."""
    drawers = [
        _drawer(wing="Wing_Theme", age_days=1),
        _drawer(wing="Wing_Privacy", age_days=1),
        _drawer(wing="Wing_Work", age_days=1),
    ]
    grouped = group_drawers_by_wing(drawers, window_days=30)
    assert "Wing_Theme" not in grouped
    assert "Wing_Privacy" not in grouped
    assert grouped.get("Wing_Work")


def test_group_drawers_keeps_drawers_with_missing_timestamp():
    """Better to over-include than under-include for theme synthesis."""
    rec = {
        "memory_space_id": "default.alice.mochi", "key": "k1", "value": "x",
        "created_at": None,
        "metadata": {"wing": "Wing_Life"},
    }
    grouped = group_drawers_by_wing([rec], window_days=30)
    assert grouped.get("Wing_Life") == [rec]


async def test_list_all_drawers_reads_pages_from_query_client():
    class _FakeQueryClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def list_drawers(self, **kwargs):
            self.calls.append(kwargs)
            offset = kwargs["offset"]
            limit = kwargs["limit"]
            rows = [
                {"key": f"k{i}", "metadata": {"wing": "Wing_Work"}}
                for i in range(offset, min(offset + limit, 5))
            ]
            return {"records": rows}

    client = _FakeQueryClient()

    rows = await _list_all_drawers(
        client,  # type: ignore[arg-type]
        memory_space_id="default.alice.mochi",
        limit=5,
        page_size=2,
    )

    assert [r["key"] for r in rows] == ["k0", "k1", "k2", "k3", "k4"]
    assert [c["offset"] for c in client.calls] == [0, 2, 4]
    assert all(c["memory_space_id"] == "default.alice.mochi" for c in client.calls)


async def test_grouped_wing_synthesis_is_bounded_parallel_and_ordered(monkeypatch):
    import asyncio

    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints import consolidator

    active = 0
    peak = 0

    async def _fake_synthesize(wing_id, records, *, settings):
        nonlocal active, peak
        del records, settings
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [Theme(wing_id, wing_id, 0.8, [f"drawer_{wing_id}"])]

    monkeypatch.setattr(consolidator, "synthesize_themes_for_wing", _fake_synthesize)
    grouped = {
        f"Wing_{index}": [{"key": f"drawer_{index}"}, {"key": f"drawer_{index}_b"}]
        for index in range(7)
    }

    rows, themes = await consolidator.synthesize_grouped_wings(
        grouped,
        settings=load_memory_settings(),
        min_drawers=2,
        confidence_threshold=0.5,
        max_parallel_wings=3,
    )

    assert peak == 3
    assert [row["wing"] for row in rows] == sorted(grouped)
    assert [theme.underlying_wing for theme in themes] == sorted(grouped)


async def test_grouped_wing_synthesis_returns_partial_results_at_pass_budget(
    monkeypatch,
):
    import asyncio

    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints import consolidator

    cancelled: list[str] = []

    async def _fake_synthesize(wing_id, records, *, settings):
        del records, settings
        if wing_id == "Wing_Fast":
            await asyncio.sleep(0.01)
            return [Theme("fast", wing_id, 0.8, ["drawer_fast"])]
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(wing_id)
            raise
        return []

    monkeypatch.setattr(consolidator, "synthesize_themes_for_wing", _fake_synthesize)
    grouped = {
        "Wing_Fast": [{"key": "drawer_fast"}],
        "Wing_Slow": [{"key": "drawer_slow"}],
    }

    rows, themes = await consolidator.synthesize_grouped_wings(
        grouped,
        settings=load_memory_settings(),
        min_drawers=1,
        confidence_threshold=0.5,
        max_parallel_wings=2,
        synthesis_budget_seconds=0.05,
    )

    by_wing = {row["wing"]: row for row in rows}
    assert by_wing["Wing_Fast"]["status"] == "completed"
    assert by_wing["Wing_Slow"]["status"] == "timed_out"
    assert by_wing["Wing_Slow"]["skipped_reason"] == "pass_budget_exhausted"
    assert [theme.text for theme in themes] == ["fast"]
    assert cancelled == ["Wing_Slow"]


async def test_grouped_wing_synthesis_isolates_unexpected_wing_failure(monkeypatch):
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.entrypoints import consolidator

    async def _fake_synthesize(wing_id, records, *, settings):
        del records, settings
        if wing_id == "Wing_Broken":
            raise RuntimeError("bad wing")
        return [Theme("ok", wing_id, 0.8, ["drawer_ok"])]

    monkeypatch.setattr(consolidator, "synthesize_themes_for_wing", _fake_synthesize)

    rows, themes = await consolidator.synthesize_grouped_wings(
        {"Wing_Broken": [{}], "Wing_Ok": [{}]},
        settings=load_memory_settings(),
        min_drawers=1,
        confidence_threshold=0.5,
        max_parallel_wings=2,
        synthesis_budget_seconds=1,
    )

    by_wing = {row["wing"]: row for row in rows}
    assert by_wing["Wing_Broken"]["status"] == "failed"
    assert "bad wing" in by_wing["Wing_Broken"]["error"]
    assert by_wing["Wing_Ok"]["status"] == "completed"
    assert [theme.text for theme in themes] == ["ok"]


def test_consolidation_status_distinguishes_partial_from_total_failure():
    from eidolon.memory.entrypoints.consolidator import _consolidation_status

    assert _consolidation_status([{"status": "completed"}]) == "completed"
    assert _consolidation_status([{"status": "skipped"}]) == "completed"
    assert _consolidation_status([
        {"status": "completed"},
        {"status": "timed_out"},
    ]) == "partial"
    assert _consolidation_status([
        {"status": "failed"},
        {"status": "timed_out"},
    ]) == "failed"


async def test_query_client_wait_until_ready_retries_no_responders(monkeypatch):
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "eidolon.memory.infrastructure.nats.query.asyncio.sleep",
        _fake_sleep,
    )

    class _FakeQueryClient(NatsMemoryQueryClient):
        def __init__(self) -> None:
            self.calls = 0

        async def list_drawers(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise NoRespondersError()
            return {"records": []}

    client = _FakeQueryClient()

    await client.wait_until_ready(
        memory_space_id="default.alice.mochi",
        timeout_seconds=1.0,
        poll_interval_seconds=0.01,
    )

    assert client.calls == 2
    assert sleeps == [0.05]


# ─── LLM-response parser ──────────────────────────────────────────────────


def test_extract_themes_clean_json():
    raw = '{"themes":[{"text":"近期担心妈妈","confidence":0.85}]}'
    out = _extract_themes_from_llm_response(raw)
    assert out == [{"text": "近期担心妈妈", "confidence": 0.85}]


def test_extract_themes_stripped_code_fence():
    raw = "```json\n{\"themes\":[{\"text\":\"x\",\"confidence\":0.7}]}\n```"
    out = _extract_themes_from_llm_response(raw)
    assert len(out) == 1 and out[0]["text"] == "x"


def test_extract_themes_garbage_returns_empty():
    assert _extract_themes_from_llm_response("not json at all") == []


def test_extract_themes_empty_array():
    assert _extract_themes_from_llm_response('{"themes":[]}') == []


# ─── _ingest_theme (cmd dispatcher) ────────────────────────────────────────


async def test_ingest_theme_writes_wing_theme_drawer():
    """``_ingest_theme`` lands the theme as a single Wing_Theme fragment.

    FakeMemoryBackend.ingest_text overrides ``metadata.source`` with its own
    marker (``"fake"``) — that's adapter-internal bookkeeping. We assert
    against the metadata that was *passed in* via ``self._inner.ingests``,
    plus the fields the adapter does preserve (``wing``, ``value``).
    """
    backend = LockedBackend(FakeMemoryBackend())
    cmd = ConsolidatorIngestThemeCommand(
        request_id="abc123def456",
        memory_space_id="default.alice.mochi",
        issued_at="2026-05-26T00:00:00Z",
        issuer="agent",
        text="近三周你担心妈妈失眠。",
        underlying_wing="Wing_Relationship",
        window_days=21,
        source_drawer_ids=["d1", "d2"],
        confidence=0.85,
    )
    await _ingest_theme(backend, cmd)

    # One Wing_Theme fragment landed.
    docs = list(backend._inner.docs.values())
    assert len(docs) == 1
    rec = docs[0]
    assert rec.metadata.get("wing") == "Wing_Theme"
    assert rec.metadata.get("underlying_wing") == "Wing_Relationship"
    assert rec.metadata.get("memory_type") == "profile"
    assert rec.value == "近三周你担心妈妈失眠。"

    # The metadata that the upstream code (turn_processor._ingest_theme)
    # passed into the backend's ingest path — this is what a real chroma
    # adapter would store verbatim. Verifies the consolidator → drawer
    # mapping is intact end-to-end.
    _, _, _, passed_meta = backend._inner.ingests[-1]
    assert passed_meta is not None
    assert passed_meta.get("source") == "consolidator"
    assert passed_meta.get("underlying_wing") == "Wing_Relationship"
    assert passed_meta.get("window_days") == 21
    assert passed_meta.get("source_drawer_ids") == ["d1", "d2"]


async def test_ingest_theme_idempotent_on_redelivery():
    """Same request_id → same fragment_id → chroma layer dedups."""
    backend = LockedBackend(FakeMemoryBackend())
    cmd = ConsolidatorIngestThemeCommand(
        request_id="dedup-key", memory_space_id="default.alice.mochi",
        issued_at="2026-05-26T00:00:00Z", issuer="agent",
        text="主题 A", underlying_wing="Wing_Work",
    )
    for _ in range(3):
        await _ingest_theme(backend, cmd)
    # FakeMemoryBackend.ingest_text stores by (wing, room); repeated calls
    # overwrite the same slot → exactly one final row.
    docs = list(backend._inner.docs.values())
    assert len(docs) == 1


# ─── Renderer [主题] section ──────────────────────────────────────────────


def _theme_record(text: str, *, underlying_wing: str = "Wing_Work") -> MemoryWireRecord:
    return MemoryWireRecord(
        memory_space_id="default.alice.mochi", key=f"theme-{abs(hash(text)) % 10000}",
        value=text,
        metadata={
            "wing": "Wing_Theme",
            "source": "consolidator",
            "underlying_wing": underlying_wing,
            "memory_type": "profile",
        },
    )


def _normal_record(text: str, *, memory_type: str = "preference") -> MemoryWireRecord:
    return MemoryWireRecord(
        memory_space_id="default.alice.mochi", key=f"frag-{abs(hash(text)) % 10000}",
        value=text,
        metadata={"memory_type": memory_type, "wing": "Wing_Profile"},
    )


def test_renderer_themes_section_label_and_position():
    """[主题] appears after [最近对话] but before vector groups."""
    themes = [_theme_record("Theme A about work", underlying_wing="Wing_Work")]
    vectors = [_normal_record("vector content about life")]
    out = group_recall_context(
        records=themes + vectors,
        kg_triples=None,
        working_memory=None,
    )
    assert "[主题]" in out, out
    # No working_memory → themes come first.
    assert out.index("[主题]") < out.index("生活方式与近况"), out


def test_renderer_themes_after_working_memory_before_vector():
    """[最近对话] → [主题] → vector groups."""
    from eidolon_memory_contracts import ConversationTurnPayload
    wm = [ConversationTurnPayload(
        turn_id="t1", user_text="u", assistant_text="a",
        timestamp="2026-05-26T00:00:00Z",
        context=MemoryActorContext(
            memory_realm_id="default.alice.mochi",
            owner_id="alice",
            companion_id="mochi",
            device_id="device-1",
            session_id="s",
        ),
    )]
    themes = [_theme_record("Theme A")]
    vectors = [_normal_record("vector content")]
    out = group_recall_context(themes + vectors, working_memory=wm)
    p_wm = out.find("[最近对话]")
    p_theme = out.find("[主题]")
    p_vec = out.find("生活方式与近况")
    assert 0 == p_wm < p_theme < p_vec, (p_wm, p_theme, p_vec, out)


def test_renderer_themes_show_underlying_wing_label():
    """Each rendered theme carries its underlying_wing in parens."""
    themes = [_theme_record("about work pressure", underlying_wing="Wing_Work")]
    out = group_recall_context(records=themes)
    assert "(Wing_Work)" in out, out


def test_renderer_themes_capped_at_4_items():
    """LLM context bound — extras are silently dropped (newest priority is
    the caller's responsibility; renderer just truncates from the head)."""
    themes = [_theme_record(f"theme-{i}") for i in range(10)]
    out = group_recall_context(records=themes)
    theme_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(theme_lines) == 4, theme_lines


def test_renderer_themes_omitted_when_none():
    """No theme records → no [主题] header pollution."""
    out = group_recall_context(records=[_normal_record("just vector")])
    assert "[主题]" not in out


def test_renderer_theme_detection_by_source_marker():
    """If ``metadata.wing`` is missing but ``source=consolidator`` is set,
    the record still routes to [主题] (defensive — survives wing renames)."""
    rec = MemoryWireRecord(
        memory_space_id="default.alice.mochi", key="x",
        value="theme content",
        metadata={"source": "consolidator", "memory_type": "profile"},
    )
    out = group_recall_context(records=[rec])
    assert "[主题]" in out


# ─── Replay safety ─────────────────────────────────────────────────────────


def test_consolidator_command_pydantic_defaults():
    """Old JetStream payloads without ``window_days`` / ``source_drawer_ids``
    must validate using defaults."""
    cmd = ConsolidatorIngestThemeCommand(
        request_id="r", memory_space_id="default.alice.mochi",
        issued_at="2026-05-26T00:00:00Z", issuer="agent",
        text="theme", underlying_wing="Wing_Work",
    )
    assert cmd.window_days == 30
    assert cmd.source_drawer_ids == []
    assert cmd.confidence == 0.7
    assert cmd.kind == "consolidator_ingest_theme"


# ─── Phase 4.1 — themes are a separate retrieval channel ───────────────────


def test_wing_theme_excluded_from_default_fanout():
    """Phase 4.1: Wing_Theme must NOT compete in the shared vector top_k.

    Broad theme summaries winning top_k slots evicted specific facts on
    precision-sensitive queries (negative / future_plans / preference),
    measured at -15..-17pp in the consolidator A/B bench. Themes reach
    recall via their own ``_fetch_themes`` channel instead.
    """
    from eidolon.memory.application.public_recall import _resolve_wings
    from eidolon.memory.config.memory_settings import load_memory_settings

    settings = load_memory_settings()
    wings = _resolve_wings(settings, wing=None, for_voice=False)
    assert "Wing_Theme" not in wings, (
        "Wing_Theme leaked into the competitive fan-out — it would evict "
        "specific facts from the shared top_k"
    )
    assert "Wing_Privacy" not in wings  # unchanged
    # The other canonical wings are still fanned out.
    assert "Wing_Profile" in wings
    assert "Wing_Emotion" in wings


def test_explicit_wing_theme_request_still_allowed():
    """An explicit ``wing="Wing_Theme"`` (e.g. the _fetch_themes channel or
    admin) bypasses the fan-out exclusion — exclusion only applies to the
    default multi-wing fan-out."""
    from eidolon.memory.application.public_recall import _resolve_wings
    from eidolon.memory.config.memory_settings import load_memory_settings

    settings = load_memory_settings()
    assert _resolve_wings(settings, wing="Wing_Theme", for_voice=False) == ["Wing_Theme"]


async def test_fetch_themes_applies_similarity_floor():
    """Phase 4.1 — themes below the relevance floor are dropped so broad
    summaries don't leak onto out-of-scope queries (the negative-category
    -20pp regression). Hits without a similarity field are kept (fakes)."""
    from unittest.mock import AsyncMock

    from eidolon.memory.application.public_recall import _fetch_themes
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.domain.wire import MemoryWireRecord

    settings = load_memory_settings().model_copy(deep=True)
    settings.recall.theme_top_k = 5
    settings.recall.theme_min_similarity = 0.55

    def _theme(val, sim):
        return MemoryWireRecord(
            memory_space_id="default.alice.mochi", key=f"k-{val}", value=val,
            metadata={"wing": "Wing_Theme", "similarity": sim},
        )

    backend = SimpleNamespace(search=AsyncMock(return_value=[
        _theme("relevant-high", 0.80),
        _theme("borderline", 0.55),     # == floor → kept
        _theme("irrelevant-low", 0.40), # < floor → dropped
    ]))
    out = await _fetch_themes(backend, "query", settings)
    vals = [r.value for r in out]
    assert vals == ["relevant-high", "borderline"], vals


async def test_fetch_themes_floor_zero_disables():
    from unittest.mock import AsyncMock

    from eidolon.memory.application.public_recall import _fetch_themes
    from eidolon.memory.config.memory_settings import load_memory_settings
    from eidolon.memory.domain.wire import MemoryWireRecord

    settings = load_memory_settings().model_copy(deep=True)
    settings.recall.theme_top_k = 5
    settings.recall.theme_min_similarity = 0.0  # disabled
    backend = SimpleNamespace(search=AsyncMock(return_value=[
        MemoryWireRecord(memory_space_id="Wing_Theme", key="k", value="low",
                         metadata={"wing": "Wing_Theme", "similarity": 0.1}),
    ]))
    out = await _fetch_themes(backend, "q", settings)
    assert [r.value for r in out] == ["low"]
