from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.palace_init import (
    PalaceInitError,
    _materialize_backend_collection,
)


def test_materialize_backend_timeout_is_palace_init_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["python", "-c", "..."],
            timeout=300.0,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(PalaceInitError) as exc:
        _materialize_backend_collection(tmp_path / "palace", backend="chroma")

    assert "materialization timed out" in str(exc.value)
    assert "partial stderr" in str(exc.value)


def test_the_palace_is_named_by_the_variable_the_cli_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positional argument never said where the palace goes.

    ``mempalace init`` reads the palace location from the environment and
    treats its positional argument as a project directory to scan. Passing the
    palace there looked right and worked wherever ``$HOME`` was writable — and
    on a Host it is not, so every palace failed to initialise and the Eidolon
    ran with no memory while every service reported healthy.
    """

    from eidolon.memory.infrastructure import palace_init

    palace = tmp_path / "palaces" / "r_1"
    seen: dict[str, object] = {}

    def _record(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setattr(palace_init, "_resolve_mempalace_cli", lambda: "/bin/mempalace")
    # Absent before the run, present after it — the shape the real check has.
    answers = iter([False, True, True])
    monkeypatch.setattr(
        palace_init, "palace_is_initialized", lambda *_a, **_k: next(answers)
    )
    monkeypatch.setattr(
        palace_init, "_materialize_backend_collection", lambda *_a, **_k: None
    )

    palace_init.ensure_palace_initialized("r_1", palace, env={"PATH": "/usr/bin"})

    env = seen["env"]
    assert env["MEMPALACE_PALACE_PATH"] == str(palace)
    # A child reaching for a home directory lands somewhere that exists rather
    # than at /nonexistent, whichever variable it happens to reach for.
    assert env["HOME"] == str(palace)
    assert env["PATH"] == "/usr/bin"


def test_an_inherited_home_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eidolon.memory.infrastructure import palace_init

    seen: dict[str, object] = {}

    def _record(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setattr(palace_init, "_resolve_mempalace_cli", lambda: "/bin/mempalace")
    answers = iter([False, True, True])
    monkeypatch.setattr(
        palace_init, "palace_is_initialized", lambda *_a, **_k: next(answers)
    )
    monkeypatch.setattr(
        palace_init, "_materialize_backend_collection", lambda *_a, **_k: None
    )

    palace_init.ensure_palace_initialized(
        "r_1",
        tmp_path / "palace",
        env={"HOME": "/home/someone"},
    )

    assert seen["env"]["HOME"] == "/home/someone"
