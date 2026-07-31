"""Static guard: the service core does not import Eidolon OS packages.

This service is releasable on its own — a memory service anyone can run with
NATS, a vector store and nothing else. That property is easy to state and easy
to lose: one convenient import of a host system's package, and the service can
no longer be built without it.

So the rule is structural. Under ``eidolon/memory/`` nothing may import
``eidolon_data``, ``eidolon_sdk`` or ``eidolon_admin``, with one exception:
``eidolon/memory/integrations/`` exists precisely to hold that wiring, ships
behind an extra, and is never imported by the core.

The protocol shared with clients lives in ``eidolon_memory_contracts``, which
this service owns, so depending on it is not a coupling to the OS.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import textwrap

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PKG_ROOT = _REPO_ROOT / "eidolon" / "memory"

# Packages belonging to Eidolon OS rather than to this service.
_OS_PACKAGES = ("eidolon_data", "eidolon_sdk", "eidolon_admin", "eidolon_hub", "eidolon_channel")

# The only place OS wiring is allowed to live.
_INTEGRATIONS = _PKG_ROOT / "integrations"


def _os_imports_in_core() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in _PKG_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if _INTEGRATIONS in path.parents:
            continue
        rel = str(path.relative_to(_REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] in _OS_PACKAGES:
                    names = ", ".join(alias.name for alias in node.names)
                    findings.append((rel, node.lineno, f"from {module} import {names}"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _OS_PACKAGES:
                        findings.append((rel, node.lineno, f"import {alias.name}"))
    return findings


def test_core_does_not_import_eidolon_os_packages() -> None:
    findings = _os_imports_in_core()
    if findings:
        listed = "\n".join(f"  {rel}:{line}    {what}" for rel, line, what in findings)
        pytest.fail(
            "The service core imports Eidolon OS packages, which makes it "
            "unreleasable on its own:\n"
            + listed
            + "\n\nMove the wiring under eidolon/memory/integrations/ and reach it "
            "through a port, or drop the dependency."
        )


def test_os_packages_are_extras_not_requirements() -> None:
    """A core import is one way to lose independence; a requirement is another."""

    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements_block = pyproject.split("[project.optional-dependencies]")[0]

    for package in _OS_PACKAGES:
        dist_name = package.replace("_", "-")
        assert f'"{dist_name}"' not in requirements_block, (
            f"{dist_name} is a hard requirement. Eidolon OS wiring belongs in the "
            "eidolon-os extra so a standalone install does not pull it in."
        )


def test_integrations_is_not_reachable_from_the_core() -> None:
    """The exemption only holds while the core never depends on it.

    Startup may *try* to build an integration and fall back when the extra is
    absent, which is a lazy import inside a function. A module-level import
    would make the core need it.
    """

    offenders: list[str] = []
    for path in _PKG_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or _INTEGRATIONS in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function_bodies = {
            id(sub)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for sub in ast.walk(node)
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if id(node) in function_bodies:
                continue
            target = node.module or "" if isinstance(node, ast.ImportFrom) else ""
            names = [target] + [alias.name for alias in node.names]
            if any(name.startswith("eidolon.memory.integrations") for name in names):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")

    assert not offenders, (
        "The core imports eidolon.memory.integrations at module level: "
        f"{offenders}. That makes the OS extra mandatory at import time."
    )


# Every module a standalone deployment actually loads: the process entrypoint
# plus the layers it pulls in. If these import with the OS packages missing, the
# service runs without them.
_STANDALONE_MODULES = (
    "eidolon.memory.entrypoints.agent_runner",
    "eidolon.memory.entrypoints.mcp_server",
    "eidolon.memory.entrypoints.supervisor",
    "eidolon.memory.entrypoints.discovery_server",
    "eidolon.memory.entrypoints.consolidator",
    "eidolon.memory.application.turn_processor",
    "eidolon.memory.application.public_recall",
    "eidolon.memory.adapters.mempalace_python_backend",
    "eidolon.memory.config",
)


def test_service_loads_with_the_os_packages_uninstalled() -> None:
    """Runtime proof, not just a source scan.

    The static checks above can be satisfied while some transitive import still
    reaches an OS package. This blocks the OS packages the way a standalone
    install would and loads the service anyway.

    It runs in a subprocess deliberately: importing the service afresh means
    clearing it out of ``sys.modules``, and doing that in-process hands every
    later test a different copy of each class, breaking isinstance checks far
    from here.
    """

    program = textwrap.dedent(
        f"""
        import builtins, sys

        blocked = {_OS_PACKAGES!r}
        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.split(".")[0] in blocked:
                raise ModuleNotFoundError("No module named " + repr(name))
            return real_import(name, *args, **kwargs)

        builtins.__import__ = refuse

        for module in {_STANDALONE_MODULES!r}:
            real_import(module, fromlist=["*"])

        leaked = sorted(m for m in sys.modules if m.split(".")[0] in blocked)
        if leaked:
            raise AssertionError("OS packages reached at runtime: " + repr(leaked))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=120,
    )

    assert result.returncode == 0, (
        "The service could not be loaded with the Eidolon OS packages absent, "
        "so it is not releasable on its own:\n" + (result.stderr or result.stdout)
    )
