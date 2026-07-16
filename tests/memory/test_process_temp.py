from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.infrastructure.process_temp import (
    configure_process_temp,
    process_temp_subprocess_env,
    resolve_process_temp_root,
)


def test_process_temp_root_defaults_beside_palace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EIDOLON_MEMORY_PROCESS_TMP_ROOT", raising=False)
    settings = MemorySettings()
    palace = tmp_path / "palaces" / "realm"

    assert resolve_process_temp_root(settings, palace) == (tmp_path / "palaces" / ".process-tmp")


def test_process_temp_root_env_overrides_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MemorySettings()
    settings.runtime.process_tmp_root = str(tmp_path / "configured")
    monkeypatch.setenv("EIDOLON_MEMORY_PROCESS_TMP_ROOT", str(tmp_path / "environment"))

    assert resolve_process_temp_root(settings, tmp_path / "palace") == (tmp_path / "environment")


def test_configure_process_temp_isolates_realms_and_resets_tempfile_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MemorySettings()
    monkeypatch.setenv("EIDOLON_MEMORY_PROCESS_TMP_ROOT", str(tmp_path / "runtime"))
    tempfile.tempdir = "/cached/host/tmp"

    first = configure_process_temp(settings, tmp_path / "palace-a", "realm-a")
    second = configure_process_temp(settings, tmp_path / "palace-b", "realm-b")

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert os.environ["TMPDIR"] == str(second)
    assert os.environ["SQLITE_TMPDIR"] == str(second)
    assert tempfile.tempdir is None


def test_process_temp_subprocess_env_is_active_before_agent_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MemorySettings()
    monkeypatch.setenv("EIDOLON_MEMORY_PROCESS_TMP_ROOT", str(tmp_path / "runtime"))

    env = process_temp_subprocess_env(
        settings,
        tmp_path / "palace",
        "realm-a",
        base_env={"KEPT": "yes", "TMPDIR": "/host/tmp"},
    )

    assert env["KEPT"] == "yes"
    assert env["TMPDIR"] == env["SQLITE_TMPDIR"]
    assert Path(env["TMPDIR"]).is_dir()
    assert Path(env["TMPDIR"]).parent == tmp_path / "runtime"
    assert env["EIDOLON_MEMORY_PROCESS_TMP_ROOT"] == str(tmp_path / "runtime")
