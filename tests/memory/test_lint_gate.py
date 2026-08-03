"""Undefined names must fail in the test suite, not at process startup.

A `NameError` from a variable that was never threaded into a scope is invisible
to every test here: `agent_runner.py` has 0% coverage because e2e runs it as a
subprocess, so 904 passing tests said nothing about whether it could start. The
way that bug was actually found was a six-minute benchmark probe failing to bind
a port.

ruff already catches it — F821 is in the configured rule set — but nothing ran
ruff. A linter configured and never executed is a linter that does not exist,
which is why this is a test rather than a note in a contributing guide.

Scoped to the rules that mean "this code is wrong", not the ones that mean "this
code is untidy". Formatting complaints in files this work never touched would
make the gate noisy, and a noisy gate gets ignored — the failure mode this is
supposed to prevent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CORRECTNESS_RULES = (
    "F821",  # undefined name — the one that shipped
    "F811",  # redefinition, so a second def silently wins
    "F841",  # assigned and never used, usually a leftover after a rewrite
    "F401",  # unused import, usually a leftover after a move
)
"""Syntax errors are not listed: ruff removed E999 as a selectable rule because it
now always reports them, so they cannot be opted out of."""


@pytest.mark.parametrize("target", ["eidolon", "tests", "contracts", "scripts"])
def test_no_correctness_lint_in(target: str) -> None:
    """One case per tree, so a failure names where to look."""

    ruff = shutil.which("ruff")
    if ruff is None:  # pragma: no cover - depends on the environment
        pytest.skip("ruff is not installed")

    result = subprocess.run(
        [
            ruff,
            "check",
            target,
            f"--select={','.join(CORRECTNESS_RULES)}",
            "--output-format=concise",
            "--no-cache",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"correctness lint failures in {target}/ — these are bugs, not style:\n"
        f"{result.stdout}{result.stderr}"
    )
