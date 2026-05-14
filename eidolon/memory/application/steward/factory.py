"""Factory for steward implementations."""

from __future__ import annotations

from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.application.steward.noop import NoOpSteward
from eidolon.memory.application.steward.rules import RuleBasedSteward
from eidolon.memory.config.memory_settings import MemorySettings


def create_steward(settings: MemorySettings):
    """Create the configured steward."""
    mode = (settings.steward.mode or "llm").strip().lower()
    if mode == "noop":
        return NoOpSteward()
    if mode == "rules":
        return RuleBasedSteward(settings)
    return LiteLLMSteward(settings, fallback=RuleBasedSteward(settings))
