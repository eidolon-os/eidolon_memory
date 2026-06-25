"""LiteLLM steward parsing and fallback behavior."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from eidolon_sdk.memory import ConversationTurnPayload, MemoryActorContext

from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.config.memory_settings import MemorySettings, load_memory_settings


def _settings_local_llm() -> MemorySettings:
    s = load_memory_settings()
    return s.model_copy(update={"llm": s.llm.model_copy(update={"model": "openai/local"})})


def _turn() -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id="t1",
        context=MemoryActorContext(
            tenant_id="default",
            owner_user_id="u1",
            persona_id="default",
            agent_id="agent",
            device_id="device",
            instance_id="instance",
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
                            "memory_space_id": "default.u1.default",
                            "source_device_id": "device",
                            "source_instance_id": "instance",
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


@pytest.mark.asyncio
async def test_llm_steward_falls_back_on_invalid_json(monkeypatch: pytest.MonkeyPatch):
    async def fake_acompletion(**_kwargs):
        return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=fake_acompletion))
    decision = await LiteLLMSteward(_settings_local_llm()).decide(_turn())
    assert decision.should_write
    assert decision.fragments[0].metadata["steward"] == "rules"
