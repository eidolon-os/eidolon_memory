"""Extraction provenance remains explicit and replay-safe."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.steward import StewardDecision

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "settings.example.yaml"


def _settings() -> MemorySettings:
    return MemorySettings.model_validate(yaml.safe_load(_CONFIG.read_text(encoding="utf-8")))


def _turn():
    from eidolon_memory_contracts import ConversationTurnPayload

    return ConversationTurnPayload.model_validate(
        {
            "turn_id": "t1",
            "timestamp": "2026-08-04T00:00:00Z",
            "user_text": "我妈张丽最近失眠",
            "assistant_text": "要注意休息",
            "context": {
                "memory_realm_id": "default.alice.default",
                "owner_id": "alice",
                "companion_id": "default",
            },
        }
    )


# ── the stamp ────────────────────────────────────────────────────────────────


def test_a_stamp_is_never_overwritten() -> None:
    """The whole mechanism.

    A steward that delegates returns the other's decision, already stamped. If the
    outer stamp won, the fallback would again be indistinguishable from a
    successful extraction — the defect this field exists to prevent.
    """

    already = StewardDecision(should_write=True, produced_by="rules:v2")

    assert already.stamped_by("llm:abc").produced_by == "rules:v2"


def test_an_unstamped_decision_takes_the_stamp() -> None:
    fresh = StewardDecision(should_write=True)

    assert fresh.stamped_by("llm:abc").produced_by == "llm:abc"


def test_stamping_does_not_mutate_the_original() -> None:
    """A caller holding the pre-stamp object must not see it change."""

    fresh = StewardDecision(should_write=True)
    fresh.stamped_by("llm:abc")

    assert fresh.produced_by == ""


async def test_llm_failure_is_not_replaced_by_a_different_extractor() -> None:
    settings = _settings()
    settings.llm.model = ""

    with pytest.raises(Exception, match="llm.model is not configured"):
        await LiteLLMSteward(settings).decide(_turn())


# ── the benchmark can now refuse a mixed corpus ──────────────────────────────


def _ledger_with(tmp_path: Path, producers: list[str]) -> Path:
    """A palace whose extraction ledger records ``producers``, one per turn."""

    palace = tmp_path / "b64_bench"
    palace.mkdir(parents=True)
    conn = sqlite3.connect(palace / "extraction_decisions.sqlite3")
    try:
        conn.execute(
            "CREATE TABLE extraction_decisions ("
            "memory_space_id TEXT, source_turn_id TEXT, extractor_version TEXT, "
            "input_hash TEXT, decision_json TEXT, intents_json TEXT, created_at TEXT)"
        )
        for index, producer in enumerate(producers):
            payload = StewardDecision(should_write=True, produced_by=producer).model_dump_json()
            conn.execute(
                "INSERT INTO extraction_decisions VALUES (?,?,?,?,?,?,?)",
                ("s", f"t{index}", "llm:same", "h", payload, "[]", "now"),
            )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


def test_a_uniform_llm_corpus_passes(tmp_path: Path) -> None:
    from scripts.benchmark.bench_memory_retrieve_quality import require_uniform_extractor

    require_uniform_extractor(_ledger_with(tmp_path, ["llm:abc"] * 40))


def test_a_single_fallback_is_enough_to_refuse(tmp_path: Path) -> None:
    """One is enough on purpose.

    The floor that caught the 2026-08-04 run needed seventeen. A run with one or
    two would have published a number describing a corpus nobody could reproduce.
    """

    from scripts.benchmark.bench_memory_retrieve_quality import require_uniform_extractor

    root = _ledger_with(tmp_path, ["llm:abc"] * 39 + ["rules:v2"])

    with pytest.raises(SystemExit) as raised:
        require_uniform_extractor(root)

    assert raised.value.code == 2


def test_an_unrecorded_producer_is_refused(tmp_path: Path) -> None:
    """Absent provenance is not evidence of a clean run.

    Reached by a decision written before the field existed. Treating it as "must
    have been the LLM" is the assumption that made the original defect invisible.
    """

    from scripts.benchmark.bench_memory_retrieve_quality import require_uniform_extractor

    root = _ledger_with(tmp_path, ["llm:abc"] * 39 + [""])

    with pytest.raises(SystemExit) as raised:
        require_uniform_extractor(root)

    assert raised.value.code == 2


def test_a_missing_ledger_is_refused(tmp_path: Path) -> None:
    from scripts.benchmark.bench_memory_retrieve_quality import require_uniform_extractor

    with pytest.raises(SystemExit) as raised:
        require_uniform_extractor(tmp_path)

    assert raised.value.code == 2


# ── the round trip through the real ledger ───────────────────────────────────


async def test_provenance_survives_the_ledger(tmp_path: Path) -> None:
    """Stored and read back without a schema change.

    ``decision_json`` holds the whole decision, so the field persists as part of
    it. Worth asserting rather than assuming: had it needed a column, every
    existing palace would have failed to open on the first write.
    """

    from eidolon.memory.domain.extraction_decision import ExtractionDecisionRecord
    from eidolon.memory.infrastructure.extraction_decisions import (
        ExtractionDecisionLedger,
    )

    ledger = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    await ledger.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id="default.alice.default",
            source_turn_id="t1",
            extractor_version="llm:policy-hash",
            input_hash="h1",
            decision=StewardDecision(should_write=True, produced_by="llm:policy-hash"),
            intents=[],
            created_at="2026-08-04T00:00:00Z",
        )
    )

    loaded = await ledger.get("default.alice.default", "t1", "llm:policy-hash")

    assert loaded is not None
    assert loaded.extractor_version == "llm:policy-hash"
    assert loaded.decision.produced_by == "llm:policy-hash"


async def test_a_decision_written_before_the_field_existed_still_loads(
    tmp_path: Path,
) -> None:
    """Replay safety: old rows have no ``produced_by`` and must not fail to parse."""

    from eidolon.memory.domain.extraction_decision import ExtractionDecisionRecord
    from eidolon.memory.infrastructure.extraction_decisions import (
        ExtractionDecisionLedger,
    )

    path = tmp_path / "decisions.sqlite3"
    ledger = ExtractionDecisionLedger(path)
    # A real write first, so the schema is created the way production creates it
    # rather than by a CREATE TABLE written here that could drift from it.
    await ledger.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id="default.alice.default",
            source_turn_id="current",
            extractor_version="llm:v1",
            input_hash="h",
            decision=StewardDecision(should_write=True, produced_by="llm:v1"),
            intents=[],
            created_at="2026-08-04T00:00:00Z",
        )
    )

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO extraction_decisions (memory_space_id, source_turn_id, "
            "extractor_version, input_hash, decision_json, intents_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "default.alice.default",
                "old",
                "llm:v1",
                "h",
                json.dumps({"should_write": True, "reason": "legacy"}),
                "[]",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    loaded = await ledger.get("default.alice.default", "old", "llm:v1")

    assert loaded is not None
    assert loaded.decision.produced_by == ""
