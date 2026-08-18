"""What the default test run promises, and what it deliberately leaves out.

A suite is only a regression signal while its result depends on this
repository. Three tests here do not: they push forty turns through a real LLM
endpoint and wait for the steward to drain them, so what they measure is that
service's throughput on the day. A full run came back red whenever it was
slow, and the cost was not those three failures — it was that the other 1121
results became unreadable.

They are deselected by default and opt-in via ``-m llm``. These tests exist so
that stays a decision rather than something that drifts back.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[1]


def _pytest_config() -> dict:
    document = tomllib.loads((_REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    return document["tool"]["pytest"]["ini_options"]


def test_the_llm_gate_is_enforced_and_not_merely_declared() -> None:
    config = _pytest_config()

    # The two used to disagree: the tests said "gated" in their docstrings and
    # every default run executed them anyway, because nothing acted on it.
    assert "llm" in config["addopts"]
    assert "not" in config["addopts"]
    assert any(entry.startswith("llm:") for entry in config["markers"])


def test_nothing_is_gated_without_saying_what_it_needs() -> None:
    declared = {entry.split(":", 1)[0] for entry in _pytest_config()["markers"]}
    reasons = {
        entry.split(":", 1)[0]: entry.split(":", 1)[1].strip()
        for entry in _pytest_config()["markers"]
    }

    # A mark that skips work has to say what would let it run, or the person
    # who sees "deselected" has no way to find out what they are missing.
    for mark in declared:
        assert reasons[mark], f"{mark} is declared with no reason"
    assert "EIDOLON_MEMORY_LLM_API_KEY" in reasons["llm"]


def test_only_the_third_party_dependency_is_deselected() -> None:
    """e2e stays in the default run; it gates on things this machine controls.

    nats-server and the agent subprocess are local, and the e2e fixtures skip
    with a reason when they are missing. Deselecting those too would trade a
    real signal for a quieter run.
    """

    addopts = _pytest_config()["addopts"]

    assert "e2e" not in addopts
    assert "mempalace" not in addopts
