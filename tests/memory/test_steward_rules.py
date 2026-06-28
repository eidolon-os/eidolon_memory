"""Rule-based steward behavior."""

from __future__ import annotations

import pytest
from eidolon_sdk.memory import ConversationTurnPayload, build_memory_actor_context

from eidolon.memory.application.steward.rules import RuleBasedSteward
from eidolon.memory.config.memory_settings import load_memory_settings


def _turn(user_text: str) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id="t1",
        context=build_memory_actor_context(
            owner_id="benchmark",
            companion_id="test",
            memory_realm_id="r:benchmark:default",
            device_id="admin-console",
            session_id="s1",
        ),
        user_text=user_text,
        assistant_text="我听到了。",
        timestamp="2026-05-14T20:00:00+08:00",
    )


def _realm_only_turn(user_text: str) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id="t1",
        context=build_memory_actor_context(
            memory_realm_id="r:benchmark:default",
            companion_id="test",
        ),
        user_text=user_text,
        assistant_text="我听到了。",
        timestamp="2026-05-14T20:00:00+08:00",
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
    decision = await RuleBasedSteward(load_memory_settings()).decide(
        _turn("我妈妈最近身体不太舒服")
    )
    assert decision.should_write
    assert decision.fragments[0].wing == "Wing_Relationship"
    assert decision.fragments[0].memory_type == "relationship"


@pytest.mark.asyncio
async def test_rules_work_wing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(
        _turn("我这个项目明天有 deadline")
    )
    assert decision.should_write
    assert decision.fragments[0].wing == "Wing_Work"
    assert decision.fragments[0].memory_space_id == "r:benchmark:default"
    assert decision.fragments[0].source_instance_id == "test"


@pytest.mark.asyncio
async def test_rules_realm_only_context_does_not_require_device(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    decision = await RuleBasedSteward(load_memory_settings()).decide(
        _realm_only_turn("这台设备在客厅，音量有点大")
    )

    assert decision.should_write
    fragment = decision.fragments[0]
    assert fragment.memory_space_id == "r:benchmark:default"
    assert fragment.scope == "persona"
    assert fragment.visibility == "all_devices"
    assert fragment.source_device_id is None
    assert fragment.target_device_id is None
    assert fragment.session_id is None


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
