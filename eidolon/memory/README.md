# Eidolon memory module (`eidolon.memory`)

**语义检索与查询（读）**：推荐通过 **Eidolon 自有 MCP read server**（`eidolon-memory-mcp` → `MemPalacePythonBackend` → MemPalace Python API），不直接向智能体暴露 MemPalace 内部 API。`MemoryService` / `MemoryClient` 只保留 legacy NATS RPC 兼容；推荐写入路径是 JetStream worker。

情感伴侣规范拓扑：**读 = 自有 MCP read server**；**写 = JetStream → `eidolon-memory-worker` → LLM/rules steward → `MemoryFragment[]` → `MemPalacePythonBackend`**。同步 NATS `MEMORY_STORE` 仍可用于兼容和本地测试，但不是推荐热路径。

完整架构见仓库根目录 [`docs/memory-architecture-plan.md`](../../docs/memory-architecture-plan.md)。

## 代码分层（阅读入口）

| 层级 | 目录 / 模块 | 职责 |
|------|----------------|------|
| **Domain** | `domain/` | `MemoryWireRecord`、`MEMORY_*` / JetStream 载荷（`payloads.py`）、后端端口 `MemoryBackend`（`ports.py`）。无 IO。 |
| **Config** | `config/` | `memory_settings.py` + `memory.default.yaml`、`palace_directory.py`、管家模板 `config/prompts/`。 |
| **Infrastructure** | `infrastructure/nats/` | JetStream `publish`。 |
| **Adapters** | `adapters/` | `MemPalacePythonBackend`、`FakeMemoryBackend`。 |
| **Application** | `application/` | `ingest.ingest_fragment`（**唯一**归一写入口）、`MemoryService`、`McpRecallClient`、`steward/`。 |
| **Entrypoints** | `entrypoints/server.py`、`entrypoints/worker.py`、`server/`（`__main__` → `-m eidolon.memory.server`） | 进程实现与 CLI 入口包。 |
| **Support** | `support/` | `logging`。 |

## 依赖

- 默认安装已包含 `faststream[nats]`、`pydantic`、`pyyaml`、`mempalace`。
- MCP 客户端：`uv sync --extra mcp`（安装 `mcp`）。
- MemPalace 本体通过 Python API 直接调用。

## 环境变量（节选）

| 变量 | 说明 |
|------|------|
| `EIDOLON_MEMORY_SETTINGS_YAML` | 主配置文件路径；不设则用包内 `memory.default.yaml` |
| `EIDOLON_MEMORY_LLM_API_KEY` | 可选：YAML 中 `llm.api_key` 为空时，从该名读取密钥（可由 `llm.api_key_env` 改名） |
| `EIDOLON_MEMORY_RUN_LIVE` / `EIDOLON_MEMORY_TEST_PALACE` | 仅 MemPalace 集成测试用 |

业务项（NATS、palace、`runtime.fake_backend`、管家、LLM 等）均在 YAML 中。`python -m eidolon.memory.server` 可传第一个参数覆盖 NATS URL，省略则用 `nats.url`。

## 进程入口

```bash
# NATS MemoryService：无 MemPalace 时在 YAML 设 runtime.fake_backend: true
export EIDOLON_MEMORY_SETTINGS_YAML=/path/to/dev-memory.yaml
python -m eidolon.memory.server
# 或：python -m eidolon.memory.server nats://127.0.0.1:4222

# JetStream 消费 Worker（需 NATS JetStream + mempalace 包）
eidolon-memory-worker
```

## 单测

```bash
uv run pytest tests -q
```

连接 **真实 MemPalace Python 包** 的用例打 `mempalace` 标记；本地请在 **仓库根目录**（`eidolon_memory/`）执行：

```bash
./scripts/run_live_memory_tests.sh
# 等价: uv run pytest tests -m mempalace -v
```

可选：`EIDOLON_MEMORY_TEST_PALACE` 指向已 `mempalace init` 的目录；不设则用临时目录并尝试自动 `init`。

## 记忆主配置

见 [`config/memory.default.yaml`](config/memory.default.yaml)；管家默认模板在 [`config/prompts/memory_steward.md`](config/prompts/memory_steward.md)。可复制后由 `EIDOLON_MEMORY_SETTINGS_YAML` 指向自定义文件。
