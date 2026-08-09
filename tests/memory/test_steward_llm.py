"""LiteLLM steward parsing and fallback behavior."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest
from eidolon_memory_contracts import ConversationTurnPayload, build_memory_actor_context

from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.config.memory_settings import MemorySettings, load_memory_settings


def _settings_local_llm() -> MemorySettings:
    s = load_memory_settings()
    return s.model_copy(update={"llm": s.llm.model_copy(update={"model": "openai/local"})})


def _turn() -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id="t1",
        context=build_memory_actor_context(
            owner_id="benchmark",
            companion_id="test",
            memory_realm_id="r:benchmark:default",
            device_id="device",
            session_id="s1",
        ),
        user_text="我喜欢晚上听轻音乐放松",
        assistant_text="我会记得这能帮你放松。",
        timestamp="2026-05-14T20:00:00+08:00",
    )


@pytest.mark.asyncio
async def test_llm_steward_accepts_valid_json(monkeypatch: pytest.MonkeyPatch):
    async def fake_acompletion(**_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": """
                        {
                          "should_write": true,
                          "reason": "有长期偏好",
                          "fragments": [{
                            "memory_space_id": "wrong.realm",
                            "source_device_id": "wrong-device",
                            "source_instance_id": "wrong-companion",
                            "wing": "Wing_Profile",
                            "room": "profile_core",
                            "content": "用户喜欢晚上听轻音乐放松。",
                            "memory_type": "preference",
                            "importance": 4,
                            "confidence": 0.9,
                            "occurred_at": "2026-05-14T20:00:00+08:00",
                            "source_turn_id": "t1",
                            "session_id": "s1",
                            "tags": ["音乐", "放松"],
                            "privacy": "normal",
                            "metadata": {}
                          }],
                          "privacy_actions": []
                        }
                        """
                    }
                }
            ]
        }

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=fake_acompletion))
    decision = await LiteLLMSteward(_settings_local_llm()).decide(_turn())
    assert decision.should_write
    assert decision.fragments[0].memory_id
    assert decision.fragments[0].metadata["steward"] == "llm"
    assert decision.fragments[0].memory_space_id == "r:benchmark:default"
    assert decision.fragments[0].memory_realm_id == "r:benchmark:default"
    assert decision.fragments[0].owner_id == "benchmark"
    assert decision.fragments[0].companion_id == "test"
    assert decision.fragments[0].source_device_id == "device"
    assert decision.fragments[0].source_instance_id == "test"
    assert decision.fragments[0].metadata["owner_id"] == "benchmark"
    assert decision.fragments[0].metadata["companion_id"] == "test"
    assert decision.fragments[0].metadata["memory_realm_id"] == "r:benchmark:default"
    assert decision.fragments[0].metadata["source_companion_id"] == "test"


@pytest.mark.asyncio
async def test_llm_steward_falls_back_on_invalid_json(monkeypatch: pytest.MonkeyPatch):
    async def fake_acompletion(**_kwargs):
        return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=fake_acompletion))
    decision = await LiteLLMSteward(_settings_local_llm()).decide(_turn())
    assert decision.should_write
    assert decision.fragments[0].metadata["steward"] == "rules"


# ── where extraction loses material ──────────────────────────────────────────
#
# Only the surviving count was observable before. So a corpus yielding few
# memories looked identical whether the model proposed little or the thresholds
# discarded most of what it proposed — and those need completely different fixes.
# The probe measured 7 fragments from 40 turns without being able to say which.


def _fragment_json(importance: int, content: str) -> str:
    return (
        '{"memory_space_id": "r:benchmark:default", "source_turn_id": "t1", '
        '"wing": "Wing_Life", "room": "colour", "content": "'
        f'{content}", "memory_type": "fact", "importance": {importance}, '
        '"confidence": 0.9}'
    )


async def _decide_with(monkeypatch, settings, fragments_json: list[str]):
    async def fake_acompletion(**_kwargs):
        body = (
            '{"should_write": true, "reason": "test", "fragments": ['
            + ", ".join(fragments_json)
            + "]}"
        )
        return {"choices": [{"message": {"content": body}}]}

    monkeypatch.setitem(
        sys.modules, "litellm", SimpleNamespace(acompletion=fake_acompletion)
    )
    return await LiteLLMSteward(settings).decide(_turn())


@pytest.mark.asyncio
async def test_low_importance_fragments_are_dropped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threshold is a real filter, not a hint.

    With min_importance 3, everything the model rated 1 or 2 is discarded. That
    is the most likely explanation for a low fragment count, and it was
    previously indistinguishable from the model proposing nothing.
    """

    settings = _settings_local_llm()
    assert settings.steward.min_importance_to_write == 3

    decision = await _decide_with(
        monkeypatch,
        settings,
        [
            _fragment_json(1, "barely worth keeping"),
            _fragment_json(2, "also below the line"),
            _fragment_json(4, "worth keeping"),
        ],
    )

    assert [f.content for f in decision.fragments] == ["worth keeping"]


@pytest.mark.asyncio
async def test_fragments_beyond_the_cap_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A separate loss path from the importance filter, and it applies first."""

    settings = _settings_local_llm()
    cap = settings.steward.max_fragments_per_turn

    decision = await _decide_with(
        monkeypatch,
        settings,
        [_fragment_json(5, f"fragment {i}") for i in range(cap + 3)],
    )

    assert len(decision.fragments) == cap


@pytest.mark.asyncio
async def test_every_loss_path_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counter is what makes a low yield diagnosable.

    Asserted against the metric rather than the log, because the metric is what
    an operator reads and what a benchmark run can be judged by.
    """

    from eidolon.memory.support import metrics

    if not metrics.METRICS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    def _count(stage: str) -> float:
        return (
            metrics.FRAGMENTS_EXTRACTED.labels(stage=stage)._value.get()  # noqa: SLF001
        )

    before = {s: _count(s) for s in ("proposed", "dropped_importance", "written")}

    await _decide_with(
        monkeypatch,
        _settings_local_llm(),
        [_fragment_json(1, "dropped"), _fragment_json(4, "kept")],
    )

    assert _count("proposed") - before["proposed"] == 2
    assert _count("dropped_importance") - before["dropped_importance"] == 1
    assert _count("written") - before["written"] == 1


# ─── identity fields belong to the turn, not to the model ─────────────────────
#
# A run on 2026-08-04 lost one turn of 40 to this: the model omitted
# ``memory_space_id``, ``MemoryFragment`` rejects a blank one, and the whole
# decision failed validation — over a field ``stamp_fragment_identity`` overwrites
# from the context a few lines later. The turn fell back to rule-based extraction
# and, because the ledger's identity is the *configured policy*, was never
# re-extracted. Zero occurrences in the 160 turns before it, which is the rate
# that makes a benchmark irreproducible rather than obviously broken.


def _ctx():
    class Ctx:
        memory_space_id = "default.alice.default"
        memory_realm_id = "default.alice.default"
        owner_id = "alice"
        companion_id = "default"
        device_id = None
        session_id = None

    return Ctx()


def _fragment(**overrides):
    base = {
        "content": "我妈失眠",
        "wing": "Wing_Relationship",
        "room": "sleep",
        "source_turn_id": "t1",
        "memory_type": "relationship",
        "importance": 4,
        "confidence": 0.9,
        "scope": "persona",
        "visibility": "all_devices",
    }
    base.update(overrides)
    return base


def _steward():
    import yaml

    from eidolon.memory.application.steward.llm import LiteLLMSteward
    from eidolon.memory.config.memory_settings import MemorySettings

    config = pathlib.Path(__file__).resolve().parents[2] / "config/settings.example.yaml"
    settings = MemorySettings.model_validate(
        yaml.safe_load(config.read_text(encoding="utf-8"))
    )
    return LiteLLMSteward(settings)


def _parse(fragment, *, context):
    import json as _json

    payload = {"should_write": True, "reason": "t", "fragments": [fragment]}
    return _steward()._parse_decision(
        _json.dumps(payload, ensure_ascii=False), context=context
    )


def test_an_omitted_space_id_is_taken_from_the_turn() -> None:
    """The case that cost a run. Validation must not reject what we overwrite."""

    decision = _parse(_fragment(), context=_ctx())

    assert decision.fragments[0].memory_space_id == "default.alice.default"


def test_a_blank_space_id_is_taken_from_the_turn() -> None:
    decision = _parse(_fragment(memory_space_id=""), context=_ctx())

    assert decision.fragments[0].memory_space_id == "default.alice.default"


def test_a_space_id_the_model_invented_is_discarded() -> None:
    """Replaced rather than defaulted, so this is not reachable.

    ``stamp_fragment_identity`` already overwrites unconditionally, so the write
    path was never at risk. Asserted here so it stays safe without depending on
    the order of two functions.
    """

    decision = _parse(_fragment(memory_space_id="default.bob.default"), context=_ctx())

    assert decision.fragments[0].memory_space_id == "default.alice.default"


def test_without_a_turn_context_a_blank_is_still_refused() -> None:
    """No context means nothing authoritative to substitute, so the old strictness
    is the right answer rather than inventing a space."""

    from eidolon.memory.domain.errors import StewardOutputError

    with pytest.raises(StewardOutputError):
        _parse(_fragment(memory_space_id=""), context=None)


# ── extensions the model wrote as prose ───────────────────────────────────────
#
# The second time the same lesson has been learned in _parse_decision: a field
# that is not the substance of a memory decided whether the memory existed. The
# first was an omitted memory_space_id; this is an annotation slot the model
# filled with a sentence.


def test_a_prose_extension_does_not_discard_the_whole_turn() -> None:
    """The exact payload that killed a 90-turn benchmark run at turn 24.

    ``extensions`` is a namespace → dict map. The model read it as a free-form
    annotation slot and wrote a sentence, which failed validation — and because
    validation is all-or-nothing, that one string discarded every fragment and
    every triple it had extracted for the turn, dropping it to rule-based
    extraction. Once in 90 turns, so in production roughly one turn in a hundred
    silently degrades and the ledger records the degraded decision as durable.
    """

    decision = _parse(
        _fragment(extensions={"note": "原话中「她」指向上一轮的张丽，未强绑定实体。"}),
        context=_ctx(),
    )

    assert decision.fragments, "the turn was discarded over an annotation"
    assert decision.fragments[0].content
    assert decision.fragments[0].extensions == {}, "the unusable entry was kept"


def test_a_usable_extension_beside_a_broken_one_survives() -> None:
    """Dropping is per entry. Losing the good annotation too would be the same
    all-or-nothing failure at a smaller scale."""

    decision = _parse(
        _fragment(
            extensions={
                "note": "一句散文",
                "kg_hint": {"entity_id": "mother:张丽"},
            }
        ),
        context=_ctx(),
    )

    assert decision.fragments[0].extensions == {"kg_hint": {"entity_id": "mother:张丽"}}


def test_an_uppercase_namespace_is_dropped_by_the_same_rule_the_domain_applies() -> None:
    """The steward strips what it cannot store, so its rule and the domain's must
    be one rule — otherwise it forwards entries that still fail the decision."""

    decision = _parse(_fragment(extensions={"KG_Hint": {"entity_id": "x"}}), context=_ctx())

    assert decision.fragments[0].extensions == {}


def test_extensions_written_as_a_list_are_dropped_not_fatal() -> None:
    decision = _parse(_fragment(extensions=["note"]), context=_ctx())

    assert decision.fragments, "the turn was discarded over an annotation"
    assert decision.fragments[0].extensions == {}


def test_a_broken_content_field_still_fails_the_decision() -> None:
    """Leniency stops at annotation. Content, wing and importance are the memory
    itself — repairing those would be inventing one."""

    from eidolon.memory.domain.errors import StewardOutputError

    with pytest.raises(StewardOutputError):
        _parse(_fragment(content="   "), context=_ctx())
