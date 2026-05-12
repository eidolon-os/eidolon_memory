"""eidolon.memory 测试共享 fixtures（位于仓库根 ``tests/memory/``）。

真实 MemPalace MCP 测试打 ``@pytest.mark.mempalace``；未设置
``EIDOLON_MEMORY_MCP_COMMAND`` 时会在 ``mempalace_runtime`` fixture 中 skip。

运行前请设置：

- ``EIDOLON_MEMORY_MCP_COMMAND``：启动 MemPalace MCP 的可执行文件（必填）
- ``EIDOLON_MEMORY_MCP_ARGS``：可选参数
- ``EIDOLON_MEMORY_TEST_PALACE``：可选，已 ``mempalace init`` 的宫殿目录；
  不设则用临时目录并尽量执行 ``mempalace init``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eidolon.memory.config.ontology import load_ontology
from eidolon.memory.infrastructure.mcp.config import McpServerLaunchConfig
from eidolon.memory.infrastructure.mcp.runtime import MemPalaceMcpRuntime


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
def live_ontology(monkeypatch: pytest.MonkeyPatch):
    """与默认 ontology 一致，但放宽 MCP 读超时（冷启动嵌入模型可能 >120ms）。"""
    monkeypatch.delenv("EIDOLON_MEMORY_ONTOLOGY_YAML", raising=False)
    ont = load_ontology()
    ont.recall.timeout_seconds = 60.0
    return ont


@pytest.fixture
def test_palace_dir(tmp_path: Path) -> Path:
    """测试用宫殿目录；可设 ``EIDOLON_MEMORY_TEST_PALACE`` 复用已有 init 目录。"""
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


@pytest.fixture
async def mempalace_runtime():
    """每个用例独立拉起/关闭 MCP 子进程（隔离宫殿时更稳）。"""
    pytest.importorskip("mcp")
    cfg = McpServerLaunchConfig.from_environ()
    if not cfg.is_configured():
        pytest.skip(
            "需要 EIDOLON_MEMORY_MCP_COMMAND（及可选 EIDOLON_MEMORY_MCP_ARGS）才能跑真实 MCP 测试",
        )
    rt = MemPalaceMcpRuntime(cfg)
    await rt.start()
    try:
        yield rt
    finally:
        await rt.stop()
