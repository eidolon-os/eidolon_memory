"""LiteLLM steward parsing and fallback behavior."""

from __future__ import annotations

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
