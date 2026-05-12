# eidolon-memory

独立仓库中的 **Eidolon 语义记忆** 包，导入路径仍为 `eidolon.memory`（可与主仓 `eidolon` 并存安装）。

**语义读**：本进程 MCP（`McpRecallClient` → `McpMemPalaceBackend`）。**写 / CRUD**：NATS `MEMORY_*` 与 JetStream 回合；`MemoryClient` 仍在 daemon 的 `eidolon.agent.shared.memory`。

## 本地安装

```bash
cd /path/to/eidolon_memory
uv sync --extra dev --extra mcp   # MCP 可选；无 MCP 可只 sync
```

## 环境变量（节选）

| 变量 | 说明 |
|------|------|
| `EIDOLON_MEMORY_MCP_COMMAND` | 启动 MemPalace MCP 的可执行文件（worker / 推荐 server） |
| `EIDOLON_MEMORY_MCP_ARGS` | 空格分隔参数 |
| `EIDOLON_MEMORY_PALACE_PATH` | 宫殿目录（最高优先级） |
| `EIDOLON_MEMORY_CONFIG_YAML` | 可选：含 `shared.mempalace.palace_path` 的 YAML 路径；相对路径相对 `EIDOLON_SHARED__DATA_DIR`（默认 `~/eidolon`） |
| `EIDOLON_SHARED__DATA_DIR` | 数据根目录，与 daemon 的 `resolve_data_dir` 一致 |
| `EIDOLON_MEMORY_ONTOLOGY_YAML` | 本体论 YAML |
| `EIDOLON_MEMORY_JS_STREAM` / `EIDOLON_MEMORY_JS_SUBJECT` / `EIDOLON_MEMORY_JS_DURABLE` | JetStream worker |
| `EIDOLON_MEMORY_FAKE_BACKEND` | `1` 时使用内存假后端 |

## 进程入口

```bash
# NATS MemoryService（仅写/CRUD；需 NATS + MCP 或 FAKE_BACKEND）
EIDOLON_MEMORY_FAKE_BACKEND=1 uv run python -m eidolon.memory.server nats://127.0.0.1:4222

# JetStream Worker
uv run eidolon-memory-worker
```

## 单测（本机）

```bash
uv run pytest tests -q
```

带 `mempalace` 标记的用例需真实 MCP：`uv run pytest tests -m mempalace -v`，或 `./scripts/run_live_memory_tests.sh`。

## 与 eidolon_daemon 联用

在 daemon 的 `pyproject.toml` 中加入依赖 `eidolon-memory`，并用 `tool.uv.sources` 指向本仓库路径（editable），删除 monorepo 内的 `eidolon/memory` 子树。

因 daemon 以可编辑方式把 `eidolon` 指向源码根目录，需在 [`eidolon/__init__.py`](file:///Users/manson/ai/eidolon/eidolon_daemon/eidolon/__init__.py) 中加入 `pkgutil.extend_path(__path__, __name__)`，才能把已安装的 `eidolon-memory` 里的 `eidolon.memory` 合并进同一顶层包。

## 代码分层

| 层级 | 目录 | 职责 |
|------|------|------|
| Domain | `eidolon/memory/domain/` | 载荷、`MemoryBackend` 端口 |
| Config | `eidolon/memory/config/` | ontology、palace_path |
| Infrastructure | `infrastructure/mcp/`、`infrastructure/nats/`、`infrastructure/bus/` | MCP、JetStream、NATS 总线薄封装 |
| Adapters | `adapters/` | MemPalace MCP、Fake |
| Application | `application/` | ingest、`MemoryService`、recall、steward |
| Entrypoints | `entrypoints/`、`server/` | server / worker |

本体与管家模板见 `eidolon/memory/config/ontology.default.yaml`、`config/prompts/memory_steward.md`。
