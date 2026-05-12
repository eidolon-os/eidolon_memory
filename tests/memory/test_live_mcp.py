"""连接真实 MemPalace MCP 的集成测试。

未设置 ``EIDOLON_MEMORY_MCP_COMMAND`` 时，依赖 ``mempalace_runtime`` 的用例会在 fixture 中 skip。
执行::

    export EIDOLON_MEMORY_MCP_COMMAND=...   # 例如 uv tool 安装的 mempalace MCP 入口
    uv run pytest tests -m mempalace -v

或 ``./scripts/run_live_memory_tests.sh``（于仓库根目录）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from eidolon.memory.adapters.mempalace_backend import McpMemPalaceBackend
from eidolon.memory.application.recall import McpRecallClient
from eidolon.memory.application.steward.noop import NoOpSteward
from eidolon.memory.domain.payloads import ConversationTurnPayload


@pytest.mark.mempalace
@pytest.mark.asyncio
async def test_mcp_server_exposes_configured_search_tool(mempalace_runtime, live_ontology):
    sess = mempalace_runtime.session
    listed = await sess.list_tools()
    names = {t.name for t in listed.tools}
    expect = live_ontology.mcp_tools.search_drawers
    if expect not in names:
        pytest.skip(
            f"MCP 未注册工具 {expect!r}（当前: {sorted(names)[:20]}…）；"
            "请用 EIDOLON_MEMORY_ONTOLOGY_YAML 对齐 MemPalace 版本",
        )
    assert expect in names


@pytest.mark.mempalace
@pytest.mark.asyncio
async def test_live_mcp_ingest_search(
    mempalace_runtime,
    live_ontology,
    test_palace_dir,
):
    backend = McpMemPalaceBackend(
        mempalace_runtime,
        live_ontology,
        str(test_palace_dir),
    )
    token = f"eidolon_mcp_live_{uuid.uuid4().hex}"
    wing = live_ontology.wings[0].id
    room = f"room_{uuid.uuid4().hex[:8]}"
    await backend.ingest_text(
        wing=wing,
        room=room,
        text=f"pytest live ingest marker {token}",
        metadata=None,
    )
    found = False
    for _ in range(80):
        hits = await backend.search(token, wing=wing, n_results=12)
        if any(token in str(h.value) for h in hits):
            found = True
            break
        await asyncio.sleep(0.5)
    assert found, "写入后语义检索在超时内未命中 marker（可检查 palace_path / 嵌入索引延迟）"


@pytest.mark.mempalace
@pytest.mark.asyncio
async def test_mcp_recall_client_roundtrip(mempalace_runtime, live_ontology, test_palace_dir):
    backend = McpMemPalaceBackend(
        mempalace_runtime,
        live_ontology,
        str(test_palace_dir),
    )
    token = f"eidolon_recall_{uuid.uuid4().hex}"
    wing = live_ontology.wings[0].id
    room = f"room_{uuid.uuid4().hex[:8]}"
    await backend.ingest_text(
        wing=wing,
        room=room,
        text=f"recall client probe {token}",
        metadata=None,
    )
    client = McpRecallClient(backend, live_ontology)
    found = False
    for _ in range(80):
        hits = await client.recall(token, wing=wing, top_k=12)
        if any(token in str(h.value) for h in hits):
            found = True
            break
        await asyncio.sleep(0.5)
    assert found


@pytest.mark.mempalace
@pytest.mark.asyncio
async def test_live_noop_steward_turn(
    mempalace_runtime,
    live_ontology,
    test_palace_dir,
):
    backend = McpMemPalaceBackend(
        mempalace_runtime,
        live_ontology,
        str(test_palace_dir),
    )
    token = f"eidolon_turn_{uuid.uuid4().hex}"
    wing = live_ontology.wings[0].id
    room = f"session_{uuid.uuid4().hex[:8]}"
    turn = ConversationTurnPayload(
        turn_id=uuid.uuid4().hex,
        user_text=f"user says {token}",
        assistant_text="assistant ack",
        timestamp="2026-05-11T12:00:00Z",
        session_id=room,
        user_id=wing,
        metadata=None,
    )
    await NoOpSteward().handle_turn(turn, backend)
    found = False
    for _ in range(80):
        hits = await backend.search(token, wing=wing, n_results=12)
        if any(token in str(h.value) for h in hits):
            found = True
            break
        await asyncio.sleep(0.5)
    assert found
