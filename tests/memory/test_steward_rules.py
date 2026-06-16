"""Rule-based steward behavior."""

from __future__ import annotations

import pytest

from eidolon.memory.application.steward.rules import RuleBasedSteward
from eidolon.memory.config.memory_settings import load_memory_settings
from eidolon_sdk.memory import ConversationTurnPayload


def _turn(user_text: str) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id="t1",
        user_text=user_text,
        assistant_text="我听到了。",
        timestamp="2026-05-14T20:00:00+08:00",
        session_id="s1",
        user_id="u1",
    )


@pytest.mark.asyncio
async def test_rules_skip_smalltalk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(_turn("你好"))
    assert not decision.should_write
    assert not decision.fragments


@pytest.mark.asyncio
async def test_rules_relationship_wing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(_turn("我妈妈最近身体不太舒服"))
    assert decision.should_write
    assert decision.fragments[0].wing == "Wing_Relationship"
    assert decision.fragments[0].memory_type == "relationship"


@pytest.mark.asyncio
async def test_rules_work_wing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(_turn("我这个项目明天有 deadline"))
    assert decision.should_write
    assert decision.fragments[0].wing == "Wing_Work"


@pytest.mark.asyncio
async def test_rules_emotion_wing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(_turn("我今天真的很焦虑"))
    assert decision.should_write
    assert decision.fragments[0].wing == "Wing_Emotion"


@pytest.mark.asyncio
async def test_rules_privacy_action_blocks_normal_write(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(_turn("这件事不要记住"))
    assert not decision.should_write
    assert not decision.fragments
    assert decision.privacy_actions[0].action == "do_not_store"
