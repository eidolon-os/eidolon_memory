# eidolon-memory

独立仓库中的 **Eidolon 语义记忆** 包，导入路径仍为 `eidolon.memory`（可与主仓 `eidolon` 并存安装）。

**语义读**：推荐使用本项目自有 MCP read server（`eidolon-memory-mcp` → `MemPalacePythonBackend`），对外不暴露 MemPalace 内部 API。**写入**：推荐 JetStream 异步 worker；legacy NATS RPC `MEMORY_*` 仅保留兼容。

完整架构基线见 [`docs/memory-architecture-plan.md`](docs/memory-architecture-plan.md)。

## 本地安装

```bash
cd /path/to/eidolon_memory
uv sync --extra dev --extra mcp   # MCP 可选；无 MCP 可只 sync
```

MemPalace 通过 Python 包直接集成，不再需要配置 MemPalace MCP 子进程。

## 环境变量（节选）

| 变量 | 说明 |
|------|------|
| `EIDOLON_MEMORY_SETTINGS_YAML` | 主配置文件路径；不设则读本地 **`memory.default.yaml`**（gitignore）；若不存在则读包内 **`memory.default.yaml.example`** |
| `EIDOLON_MEMORY_LLM_API_KEY` | 可选：当 YAML 中 `llm.api_key` 为空时，从该环境变量读取密钥（名称可由 YAML 的 `llm.api_key_env` 修改） |
| `EIDOLON_MEMORY_RUN_LIVE` | 仅集成测试：设为 `1` 时运行真实 MemPalace 用例 |
| `EIDOLON_MEMORY_TEST_PALACE` | 仅测试：指向已 `mempalace init` 的目录 |

NATS、palace 路径、管家模式、LLM 端点、`runtime.fake_backend` 等均在 YAML 中配置。`python -m eidolon.memory.server` 可选第一个命令行参数覆盖 NATS 地址；省略时使用 YAML 的 `nats.url`。

## 进程入口

```bash
# NATS MemoryService：无 MemPalace 时把自定义 YAML 里 runtime.fake_backend 设为 true
export EIDOLON_MEMORY_SETTINGS_YAML=/path/to/dev-memory.yaml
uv run python -m eidolon.memory.server
# 或显式指定 NATS：uv run python -m eidolon.memory.server nats://127.0.0.1:4222

# JetStream Worker
uv run eidolon-memory-worker

# 自有 MCP read server
uv run eidolon-memory-mcp
```

开发阶段：在同目录执行 `cp eidolon/memory/config/memory.default.yaml.example eidolon/memory/config/memory.default.yaml`，再编辑后者（**仅此文件承载你的真实配置，且不提交 git**）。也可用 `EIDOLON_MEMORY_SETTINGS_YAML` 指向任意路径。

```yaml
runtime:
  palace_path: "/Users/manson/eidolon/mempalace"
  fake_backend: false
nats:
  url: "nats://127.0.0.1:4222"
  stream: "MEMORY_TURNS"
  subject: "agent.memory.conversation.turn"
  durable: "eidolon-memory-worker"
steward:
  mode: "llm"
llm:
  model: "openai/local-model"
  base_url: "http://127.0.0.1:1234/v1"
  api_key: ""
  api_key_env: "EIDOLON_MEMORY_LLM_API_KEY"
```

真实场景脚本：

```bash
.venv/bin/python scripts/live_config_check.py
scripts/live_start_worker.sh
.venv/bin/python scripts/live_publish_turn_case.py emotion_work
.venv/bin/python scripts/live_recall_case.py "用户最近为什么焦虑，什么能让他放松？"
```

本地 OpenAI-compatible LLM：把上表写入自定义 YAML，设置 `EIDOLON_MEMORY_SETTINGS_YAML` 指向该文件；密钥放在 `llm.api_key` 或 `EIDOLON_MEMORY_LLM_API_KEY`（与 `api_key_env` 一致即可）。

## 单测（本机）

```bash
uv run pytest tests -q
```

带 `mempalace` 标记的用例需真实 MemPalace Python 包：`uv run pytest tests -m mempalace -v`，或 `./scripts/run_live_memory_tests.sh`。

## 与 eidolon_daemon 联用

在 daemon 的 `pyproject.toml` 中加入依赖 `eidolon-memory`，并用 `tool.uv.sources` 指向本仓库路径（editable），删除 monorepo 内的 `eidolon/memory` 子树。

因 daemon 以可编辑方式把 `eidolon` 指向源码根目录，需在 [`eidolon/__init__.py`](file:///Users/manson/ai/eidolon/eidolon_daemon/eidolon/__init__.py) 中加入 `pkgutil.extend_path(__path__, __name__)`，才能把已安装的 `eidolon-memory` 里的 `eidolon.memory` 合并进同一顶层包。

## 代码分层

| 层级 | 目录 | 职责 |
|------|------|------|
| Domain | `eidolon/memory/domain/` | 载荷、`MemoryBackend` 端口 |
| Config | `eidolon/memory/config/` | `memory_settings`、`palace_directory`、管家模板 |
| Infrastructure | `infrastructure/nats/`、`infrastructure/bus/` | JetStream、NATS 总线薄封装 |
| Adapters | `adapters/` | MemPalace Python API、Fake |
| Application | `application/` | ingest、`MemoryService`、recall、steward |
| Entrypoints | `entrypoints/`、`server/` | server / worker |

默认模板见 `eidolon/memory/config/memory.default.yaml.example`（仓库内）；本地在旁创建 **`memory.default.yaml`**（gitignore）作为你唯一要维护的配置。管家模板见 `config/prompts/memory_steward.md`。
