"""Steward factory selection."""

from __future__ import annotations

import pytest

from eidolon.memory.application.steward import LiteLLMSteward, NoOpSteward
from eidolon.memory.application.steward.factory import create_steward
from eidolon.memory.config.memory_settings import MemorySettings, load_memory_settings


def _with_steward_mode(mode: str) -> MemorySettings:
    s = load_memory_settings()
    return s.model_copy(update={"steward": s.steward.model_copy(update={"mode": mode})})


def test_factory_rejects_removed_keyword_steward():
    with pytest.raises(ValueError, match="unsupported steward mode"):
        create_steward(_with_steward_mode("rules"))


def test_factory_rejects_test_steward_without_explicit_test_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_TEST_STEWARD", raising=False)

    with pytest.raises(ValueError, match="unsupported steward mode"):
        create_steward(_with_steward_mode("test-verbatim"))


def test_factory_selects_noop():
    steward = create_steward(_with_steward_mode("noop"))
    assert isinstance(steward, NoOpSteward)


def test_factory_defaults_to_llm():
    steward = create_steward(load_memory_settings())
    assert isinstance(steward, LiteLLMSteward)
