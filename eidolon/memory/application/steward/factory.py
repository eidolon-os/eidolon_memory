"""Factory for steward implementations."""

from __future__ import annotations

import os

from eidolon.memory.application.steward.llm import LiteLLMSteward
from eidolon.memory.application.steward.noop import NoOpSteward
from eidolon.memory.application.steward.verbatim_test import VerbatimTestSteward
from eidolon.memory.config.memory_settings import MemorySettings


def create_steward(settings: MemorySettings):
    """Create the configured steward."""
    mode = (settings.steward.mode or "llm").strip().lower()
    if mode == "noop":
        return NoOpSteward()
    if mode == "llm":
        return LiteLLMSteward(settings)
    if mode == "test-verbatim" and os.environ.get("EIDOLON_MEMORY_TEST_STEWARD") == "1":
        return VerbatimTestSteward(settings)
    raise ValueError(f"unsupported steward mode: {mode}")
