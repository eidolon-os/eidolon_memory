"""Static guard:internal cross-package lazy imports are banned.

Why:long-lived `agent_runner` processes + `from eidolon.memory.X import Y`
inside a function body can race with on-disk edits. The Python module cache
(`sys.modules`) can end up with OLD `public_recall` lazy-importing a NEW
`kg_recall` that no longer exports the symbol → ImportError at first MCP
call. Real example:commit ``ecde449`` removed
``extract_entity_candidates`` but the lazy import in the *cached* OLD
``public_recall`` kept asking for it after the file change.

Allowed lazy imports:
- stdlib only (``import asyncio`` / ``from pathlib import Path``)
- 3rd-party packages (``mempalace.*``, ``litellm``, ``nats``, ``uvicorn`` …)

Disallowed lazy imports:
- anything whose module path starts with ``eidolon.memory.``

The guard runs at every PR; adding a new internal lazy fails the build.
If you have a legitimate circular-dependency reason, add to ``ALLOWLIST``
below with the rationale.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PKG_ROOT = _REPO_ROOT / "eidolon" / "memory"

# Pairs of (file_relative_to_repo, lineno) — explicit waivers with justification.
# Empty today (Phase 0 cleaned all 13 occurrences). Add only with a one-line
# comment explaining the real circular-import constraint.
ALLOWLIST: set[tuple[str, int]] = set()


def _collect_internal_lazy_imports() -> list[tuple[str, int, str]]:
    """AST-walk every .py under eidolon/memory/ and find function-body imports
    whose module path begins with ``eidolon.memory.``.

    Returns ``(rel_path, lineno, target)`` triples for any violation.
    """
    findings: list[tuple[str, int, str]] = []
    for path in _PKG_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(_REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    mod = sub.module or ""
                    if mod.startswith("eidolon.memory"):
                        names = ", ".join(n.name for n in sub.names)
                        findings.append((rel, sub.lineno, f"from {mod} import {names}"))
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        if alias.name.startswith("eidolon.memory"):
                            findings.append((rel, sub.lineno, f"import {alias.name}"))
    return findings


def test_no_internal_lazy_imports() -> None:
    """Phase 0 invariant — no cross-package `eidolon.memory.*` lazy imports.

    See module docstring for the rationale (commit ``ecde449``).
    """
    findings = _collect_internal_lazy_imports()
    actual_violations = [
        (rel, lineno, target)
        for rel, lineno, target in findings
        if (rel, lineno) not in ALLOWLIST
    ]
    if actual_violations:
        lines = "\n".join(
            f"  {rel}:{lineno}    {target}" for rel, lineno, target in actual_violations
        )
        msg = (
            "Found cross-package lazy imports under eidolon/memory/ "
            "(see tests/memory/test_lazy_import_guard.py docstring):\n"
            + lines
            + "\n\nMove these to module-level imports, or add an explicit "
            + "ALLOWLIST entry with a circular-dependency justification."
        )
        pytest.fail(msg)


def test_allowlist_entries_match_real_imports() -> None:
    """Catch stale ALLOWLIST entries — if a waiver no longer corresponds
    to a real lazy import in the code, the waiver itself becomes a footgun.
    """
    findings_locations = {(rel, lineno) for rel, lineno, _ in _collect_internal_lazy_imports()}
    stale = ALLOWLIST - findings_locations
    if stale:
        pytest.fail(
            "ALLOWLIST has stale entries (no longer reference a real lazy "
            f"import): {sorted(stale)}.  Remove them."
        )
