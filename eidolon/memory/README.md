# Eidolon memory module (`eidolon.memory`)

**语义检索与查询（读）**：仅通过 **本进程 MCP**（`McpRecallClient` → `McpMemPalaceBackend` → MemPalace MCP Server），**不在本模块内**经 NATS 提供读 RPC。`MemoryService` / `MemoryClient` 只承载 **写入与 CRUD**（`MEMORY_STORE`、`MEMORY_GET` 等）及 JetStream 回合投递。

情感伴侣规范拓扑：**读 = 主进程 MCP**；**写** 两条可选路径——**同步**经 NATS `MEMORY_STORE` → `MemoryService`，或 **异步**经 JetStream → `eidolon-memory-worker`；二者落到存储时均调用同一 **`application.ingest.ingest_fragment`**，再进入 `McpMemPalaceBackend.ingest_text`（同一 MCP 写实现）。

## 代码分层（阅读入口）

| 层级 | 目录 / 模块 | 职责 |
|------|----------------|------|
| **Domain** | `domain/` | `MemoryWireRecord`、`MEMORY_*` / JetStream 载荷（`payloads.py`）、后端端口 `MemoryBackend`（`ports.py`）。无 IO。 |
| **Config** | `config/` | `ontology.py` + `ontology.default.yaml`、`palace_path.py`、管家模板 `config/prompts/`。 |
| **Infrastructure** | `infrastructure/mcp/`、`infrastructure/nats/` | MCP stdio 会话、JetStream `publish`。 |
| **Adapters** | `adapters/` | MCP JSON 解析、`McpMemPalaceBackend`、`FakeMemoryBackend`。 |
| **Application** | `application/` | `ingest.ingest_fragment`（**唯一**归一写入口）、`MemoryService`、`McpRecallClient`、`steward/`。 |
| **Entrypoints** | `entrypoints/server.py`、`entrypoints/worker.py`、`server/`（`__main__` → `-m eidolon.memory.server`） | 进程实现与 CLI 入口包。 |
| **Support** | `support/` | `logging`。 |

## 依赖

- 默认安装已包含 `faststream[nats]`、`pydantic`、`pyyaml`。
- MCP 客户端：`uv sync --extra mcp`（安装 `mcp`）。
- MemPalace 本体由 **MCP Server 子进程** 提供（不在此包内 `import mempalace` 写库）。

## 环境变量（节选）

| 变量 | 说明 |
|------|------|
| `EIDOLON_MEMORY_MCP_COMMAND` | 启动 MemPalace MCP 的可执行文件（必填，worker / 推荐 server） |
| `EIDOLON_MEMORY_MCP_ARGS` | 空格分隔参数 |
| `EIDOLON_MEMORY_PALACE_PATH` | 宫殿目录（覆盖 YAML `shared.mempalace.palace_path`） |
| `EIDOLON_MEMORY_ONTOLOGY_YAML` | 本体论 + 工具名映射 YAML 路径 |
| `EIDOLON_MEMORY_JS_STREAM` | JetStream stream 名（设置后 `MemoryClient.publish_conversation_turn` 生效） |
| `EIDOLON_MEMORY_JS_SUBJECT` | subject（默认 `agent.memory.conversation.turn`） |
| `EIDOLON_MEMORY_JS_DURABLE` | Worker consumer durable 名 |
| `EIDOLON_MEMORY_FAKE_BACKEND` | `1` 时 `memory.server` 使用内存假后端（单测/本地无 MCP） |

## 进程入口

```bash
# Legacy NATS RPC MemoryService（仅写/CRUD；需 NATS + MCP 或 FAKE_BACKEND）
EIDOLON_MEMORY_FAKE_BACKEND=1 python -m eidolon.memory.server nats://127.0.0.1:4222

# JetStream 消费 Worker（需 NATS JetStream + MCP）
eidolon-memory-worker
```

## 单测

```bash
uv run pytest tests -q
```

连接 **真实 MemPalace MCP** 的用例打 `mempalace` 标记；未设置 `EIDOLON_MEMORY_MCP_COMMAND` 时会在 fixture 中 skip。本地请在 **仓库根目录**（`eidolon_memory/`）执行：

```bash
export EIDOLON_MEMORY_MCP_COMMAND=...   # MemPalace MCP 启动命令
./scripts/run_live_memory_tests.sh
# 等价: uv run pytest tests -m mempalace -v
```

可选：`EIDOLON_MEMORY_TEST_PALACE` 指向已 `mempalace init` 的目录；不设则用临时目录并尝试自动 `init`。

## 本体论配置

见 [`config/ontology.default.yaml`](config/ontology.default.yaml)；管家默认模板在 [`config/prompts/memory_steward.md`](config/prompts/memory_steward.md)。可复制后由 `EIDOLON_MEMORY_ONTOLOGY_YAML` 指向自定义文件。
