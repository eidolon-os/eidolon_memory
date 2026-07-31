"""Recall budgets, and the concurrency the non-voice path actually gets."""

from __future__ import annotations

import pytest

from eidolon.memory.application.public_recall import _effective_wing_parallel
from eidolon.memory.config.memory_settings import MemorySettings


def _settings(**recall) -> MemorySettings:
    return MemorySettings.model_validate({"recall": recall} if recall else {})


def test_the_non_voice_graph_budget_is_configurable_and_sane() -> None:
    """It was a hardcoded second — longer than a whole chat retrieval budget."""

    assert _settings().recall.kg_timeout_seconds_normal == 0.3
    assert _settings(kg_timeout_seconds_normal=0.6).recall.kg_timeout_seconds_normal == 0.6


def test_voice_keeps_the_tighter_budget() -> None:
    settings = _settings()

    assert settings.recall.kg_timeout_seconds < settings.recall.kg_timeout_seconds_normal


@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        (1, 1),  # never zero
        (2, 1),
        (4, 2),
        (8, 4),  # capped
        (32, 4),
        (None, 2),  # cpu_count() can return None
    ],
)
def test_non_voice_fan_out_uses_half_the_cores_capped_at_four(
    monkeypatch: pytest.MonkeyPatch, cores: int | None, expected: int
) -> None:
    """Regression: the halving used to be unreachable.

    The expression was `min(4, os.cpu_count() or 4 // 2)`, and `//` binds tighter
    than `or`, so it evaluated as `os.cpu_count() or 2` — the full core count on
    any machine that reports one. A non-voice recall would fan out wider than
    intended on a box also serving voice.
    """

    monkeypatch.setattr("eidolon.memory.application.public_recall.os.cpu_count", lambda: cores)

    assert _effective_wing_parallel(_settings(), for_voice=False) == expected


def test_an_explicit_fan_out_setting_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eidolon.memory.application.public_recall.os.cpu_count", lambda: 32)
    settings = MemorySettings.model_validate({"runtime": {"read": {"max_wing_parallel": 7}}})

    assert _effective_wing_parallel(settings, for_voice=False) == 7
