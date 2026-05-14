"""Steward factory selection."""

from __future__ import annotations

from eidolon.memory.application.steward import LiteLLMSteward, NoOpSteward, RuleBasedSteward
from eidolon.memory.application.steward.factory import create_steward
from eidolon.memory.config.memory_settings import MemorySettings, load_memory_settings


def _with_steward_mode(mode: str) -> MemorySettings:
    s = load_memory_settings()
    return s.model_copy(update={"steward": s.steward.model_copy(update={"mode": mode})})


def test_factory_selects_rules():
    steward = create_steward(_with_steward_mode("rules"))
    assert isinstance(steward, RuleBasedSteward)


def test_factory_selects_noop():
    steward = create_steward(_with_steward_mode("noop"))
    assert isinstance(steward, NoOpSteward)


def test_factory_defaults_to_llm():
    steward = create_steward(load_memory_settings())
    assert isinstance(steward, LiteLLMSteward)
