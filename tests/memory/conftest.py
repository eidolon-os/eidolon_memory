"""eidolon.memory 测试共享 fixtures。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eidolon.memory.config.memory_settings import (
    get_memory_settings,
    reset_memory_settings_cache,
)


@pytest.fixture(autouse=True)
def _reset_default_memory_settings_cache() -> None:
    """每个用例开始前清空默认路径缓存，避免 env 或文件与上一用例串味。"""
    reset_memory_settings_cache()
    yield


def _maybe_init_palace(palace: Path) -> None:
    exe = shutil.which("mempalace")
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "init", str(palace)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


@pytest.fixture
def live_memory_settings(monkeypatch: pytest.MonkeyPatch):
    """与默认 memory settings 一致，但放宽检索读超时（冷启动嵌入模型可能较慢）。"""
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = get_memory_settings().model_copy(deep=True)
    settings.recall.timeout_seconds = 60.0
    return settings


@pytest.fixture
def test_palace_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """测试用宫殿目录；可设 ``EIDOLON_MEMORY_TEST_PALACE`` 复用已有 init 目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    raw = os.environ.get("EIDOLON_MEMORY_TEST_PALACE", "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        _maybe_init_palace(p)
        return p
    p = tmp_path / "mempalace_test_palace"
    p.mkdir(parents=True, exist_ok=True)
    _maybe_init_palace(p)
    return p
