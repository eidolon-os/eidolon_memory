# 外部如何调用 `eidolon-memory`

本文说明**宿主进程 / 智能体 / 其它服务**在仓库外如何与本项目集成：**读通道**、**写通道**以及可选的运维 API。语义与分层细节仍以 [memory-architecture-plan.md](memory-architecture-plan.md) 为准。

---

## 1. 能力一览（选型指引）

| 场景 | 推荐方式 | 需要运行的进程 |
|------|----------|----------------|
| Agent / IDE 语义检索回忆 | **MCP**：`eidolon-memory-mcp` | MCP 服务端 + （通常）已通过 `python -m eidolon.memory.server` 打开的 MemPalace 写入面；语义读可走 MCP 单机进程 |
| 对话结束后结构化写入（热路径） | **JetStream**：往 YAML 配置的 subject 发 `ConversationTurnPayload` JSON | **`eidolon-memory-worker`** + NATS JetStream |
| 简单同步写入 / 按 drawer 读写删 | **NATS Core**：`MEMORY_STORE` / `GET` / … | **`python -m eidolon.memory.server`** |
| 同进程嵌入式 | **Python API**：`McpRecallClient` / `ingest_fragment` 等 | 仅依赖导入 `eidolon.memory` |
| 本机可视化列表与层级浏览 | **Admin HTTP**（`admin/`） | `uvicorn` + `vite`（见 `admin/run_all.sh`）；**非**对外推荐的生产协议 |

语义检索**不推荐**再走已废弃或历史的 `MEMORY_QUERY` 思路；请以 MCP 或服务内 `McpRecallClient` + `MemoryBackend.search` 为准（与 MCP 对齐逻辑见 `eidolon.memory.application.public_recall`）。

---

## 2. 环境与会话前置

```bash
cd /path/to/eidolon_memory
uv sync --extra dev --extra mcp   # 仅 MCP 需加 --extra mcp
```

典型环境变量与子进程说明见仓库根目录 [README.md](../README.md)。配置优先级简要回顾：

1. `EIDOLON_MEMORY_SETTINGS_YAML`（若指定）
2. 否则本地 `memory.default.yaml`（若存在）
3. 否则包内 **`memory.default.yaml.example`**（或等价 bundled 示例）

宫殿目录由配置中的 `runtime.palace_path` 再通过 `resolve_palace_directory` 解析（未填则默认用户目录下的 `eidolon/mempalace`）。

---

## 3. 语义读 — MCP Read Server（对外主路径）

### 启动

```bash
export EIDOLON_MEMORY_SETTINGS_YAML=/optional/path/to/memory.yaml   # 可选
uv run eidolon-memory-mcp
```

宿主（如 Cursor / Claude Desktop / 自研网关）将该进程注册为标准 **MCP stdio**，使用包名 `eidolon-memory`（实现见 `eidolon/memory/entrypoints/mcp_server.py`）。

### 提供的 Tools（摘要）

工具名固定为下列三个（不要在客户端自拟名称）：

1. **`eidolon_memory_search`**  
   - 参数：`query`, `user_id`, `top_k`, 可选 `wing`, `room`  
   - 行为：在多翼上语义检索，与用户可见性规则与排序策略一致。

2. **`eidolon_memory_recall_context`**  
   - 参数：`query`, `user_id`, `top_k`  
   - 行为：同上，额外返回拼装好的可读 `context` 文本块。

3. **`eidolon_memory_status`**  
   - 返回：后端类型、`palace_path`、管家模式、`wings` 配置快照等运维信息。

宿主侧只要把用户身份映射到 `user_id` 字符串，并控制 `wing`/`room` 细粒度可选过滤即可。**不要**直接向智能体暴露 MemPalace/Chroma 的原始 API。

---

## 4. 异步写入 — NATS JetStream（推荐生产写路径）

### 运行时组成

1. **`eidolon-memory-worker`** 消费 JetStream；
2. 外部发布者只需连 **同一 NATS**，向 YAML 里的 **`nats.stream` / `nats.subject`** 发布消息；
3. 载荷为 **单行 JSON**：`ConversationTurnPayload`（Pydantic 模型见 `eidolon/memory/domain/payloads.py`）。

### 载荷字段（`ConversationTurnPayload`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `turn_id` | string | 必填，唯一回合 id |
| `user_text` | string | 用户侧文本 |
| `assistant_text` | string | 助手侧文本 |
| `timestamp` | string | ISO 时间或其它统一约定字符串 |
| `session_id` | string | 可选 |
| `user_id` | string | 可选，管家切分写入时常用 companion 租户 id |
| `metadata` | object | 可选，附加键值 |

### 发布示例（抽象）

可使用项目内 **`JetStreamTurnPublisher`**（`eidolon.memory.infrastructure.nats.turns`）从已加载的 `MemorySettings` 构造，调用 `publish_turn(payload)`；或自行调用 `JetStream.publish(subject, JSON bytes)`。

**注意：** Core NATS Subject 常量 `SharedSubjects.MEMORY_CONVERSATION_TURN` 与 YAML 默认 `subject: agent.memory.conversation.turn` 应对齐；以外部发布为准请以 **YAML `nats.subject`** 为目标。

---

## 5. 同步读写 — NATS Core `MemoryService`（兼容）

本路径由 **`python -m eidolon.memory.server`** 拉起，仅在 **同一 NATS 集群**上使用下列 **Subject**（定义于 `eidolon/memory/infrastructure/bus/subjects.py`）：

| Subject | 方向 | 载荷（`envelope.payload`） |
|---------|------|----------------------------|
| `agent.memory.store` | Client → MemoryService | **MemoryStorePayload** |
| `agent.memory.get` | Client → MemoryService | **MemoryGetPayload** |
| `agent.memory.get_all` | Client → MemoryService | **MemoryGetAllPayload** |
| `agent.memory.delete` | Client → MemoryService | **MemoryDeletePayload** |
| `agent.memory.result` | MemoryService → Client（回复） | **MemoryResultPayload** |

外层仍需符合 **`BusEnvelope`**（`header` + `payload`），与主仓 Agent 使用的总线协议一致：

```python
BusEnvelope(
    header=BusHeader(source="your-component", msg_type="..."),
    payload=MemoryStorePayload(...).model_dump(),
)
```

各 Payload 字段见 **`eidolon/memory/domain/payloads.py`**。简述 **写入**：

- `MemoryStorePayload.text`：正文；
- `wing` 或 `user_id`（二选一兜底）：在服务实现里映射为 ingest 使用的 **翼名**；
- `room`：映射为阁名。

回复通过 **`agent.memory.result`**（`MEMORY_RESULT`）主题按 `reply_to` 回传（或由 FastStream/NATS request-reply 机制投递到收件箱）。

---

## 6. Python 进程内调用（嵌入式）

在安装本包的环境中可直接：

```python
from eidolon.memory.application.recall import McpRecallClient
from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory

settings = get_memory_settings()
backend = MemPalacePythonBackend(settings, str(resolve_palace_directory(settings)))
client = McpRecallClient(backend, settings)

# asyncio
hits = await client.recall("用户最近提到过什么？", wing="Wing_Profile")
```

写入请走 **`ingest_fragment`** / **`ingest_memory_fragment`**（见 `eidolon/memory/application/ingest.py`），与 Steward、NATS STORE 同源归一入口。

如需与 MCP 完全一致的跨翼语义检索，可使用 **`search_all_wings_mcp_style`**（`eidolon/memory/application/public_recall.py`）。

---

## 7. Admin HTTP（本地运维）

路径：`admin/server` + `admin/web`，脚本 **`admin/run_all.sh`**。默认 **`GET http://127.0.0.1:8010/docs`** OpenAPI。

主要前缀 **`/api`**：

- **`GET /api/health`**：存活与 palace 概要；
- **`GET /api/memories`**：列表（可选租户过滤）；
- **`GET /api/memories/search`**：与 MCP 同策略的语义搜索；
- **`POST /api/memories`**：`ingest_fragment` 等价写入；
- **`DELETE /api/memories/{key}`**：`key` 为 MemPalace 的 `drawer_*` id；
- **`GET /api/hierarchy`**：MemPalace 四层结构与观测树。

可按环境变量 `EIDOLON_MEMORY_ADMIN_TOKEN` 启用 Bearer 校验。该接口定位为 **本机运维 / 演示**，不向公网假设安全模型。

---

## 8. 相关文档索引

| 文档 | 内容 |
|------|------|
| [memory-architecture-plan.md](memory-architecture-plan.md) | Wings/Room 语义、管家、召回策略 |
| [../README.md](../README.md) | 安装环境变量与各进程一键命令 |

若与 **eidolon_daemon / 宿主 Agent** 联用时的包命名空间合并，请参阅仓库 README 「与 eidolon_daemon 联用」小节。
