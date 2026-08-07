"""Static guard: the service core does not import Eidolon OS packages.

This service is releasable on its own — a memory service anyone can run with
NATS, a vector store and nothing else. That property is easy to state and easy
to lose: one convenient import of a host system's package, and the service can
no longer be built without it.

So the rule is structural, and as of 2026-08-07 it has **no exception**. Under
``eidolon/memory/`` nothing may import ``eidolon_data``, ``eidolon_sdk`` or
``eidolon_admin``, anywhere.

There used to be one: ``eidolon/memory/integrations/`` held wiring for a host
system, behind an extra, never imported by the core. It was deleted because both
adapters in it were written against an eidolon_data interface that repository has
since replaced, and neither was reachable from any production path — one had no
callers at all and the other's only wiring point passed ``None``. The exception
was a place for that to sit unnoticed, so removing it makes this guard stricter
rather than weaker.

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

def _os_imports_in_core() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in _PKG_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
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


def test_no_os_package_is_a_dependency_at_all() -> None:
    """A core import is one way to lose independence; a dependency is another.

    This used to allow OS packages inside an extra, on the reasoning that an
    optional dependency costs a standalone install nothing. True, and it still
    left a place for host wiring to accumulate unnoticed — which is exactly what
    happened: the ``eidolon-os`` extra carried an integration nothing reachable
    ever called, written against an interface the other repository had already
    replaced. Now they may not appear anywhere in the dependency surface.
    """

    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = pyproject.split("[tool.")[0]  # dependencies + extras, not tooling

    for package in _OS_PACKAGES:
        dist_name = package.replace("_", "-")
        assert f'"{dist_name}"' not in manifest, (
            f"{dist_name} is declared as a dependency. This service is releasable "
            "on its own; host wiring belongs in the host."
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
