# eidolon-memory

陪伴智能体的语义记忆服务。提供向量召回 + bi-temporal 知识图谱融合,300ms 硬预算,
以 MCP / NATS / 同进程 三条契约对外暴露。

> 完整设计:[`docs/memory-architecture-plan.md`](docs/memory-architecture-plan.md)(架构基线)、
> [`docs/architecture-d1-readwrite-split.md`](docs/architecture-d1-readwrite-split.md)(D1 进程拓扑)、
> [`docs/plan-kg-integration.md`](docs/plan-kg-integration.md)(KG 集成)。
> 本 README 是**外部集成方**的快速入口。

---

## 1. 这是什么 / 给谁用

| 你是 | 走哪条 |
|------|--------|
| LiveKit voice 主进程 / 同 monorepo 的 Python | **同进程 API**(import,零开销,300ms 含 ONNX) |
| 外部 Python / Node / Cursor / Claude IDE | **MCP Streamable HTTP**(默认 `http://127.0.0.1:8030/mcp`) |
| 写一条对话后异步落盘(steward 后台抽取) | **NATS JetStream**(发 `ConversationTurnPayload`) |
| eidolon-agent 启动 / 周期刷新路由 | **Discovery HTTP**(`http://127.0.0.1:8020/api/discovery/agent-routing`) |

读写都最终经同一个 `LockedBackend` + `LockedKnowledgeGraph`(单个 `asyncio.Lock` 串行 chromadb + KG SQLite 调用),保证 D1 single-owner-per-palace 不变量。

---

## 2. 架构一图

```
┌───────────────────────────────────────────────────────────────────┐
│ eidolon-memory-supervisor  (Python,纯进程经理,subprocess.Popen)  │
│   │                                                                │
│   ├─ eidolon-memory-agent --user-id=alice --port=8030 ────────────┤
│   │     ├─ LiveKit pipeline (in-process recall)                    │
│   │     ├─ MCP Streamable HTTP @ 127.0.0.1:8030/mcp               │
│   │     ├─ NATS subscriber  agent.memory.conversation.turn.alice   │
│   │     │                   agent.memory.cmd.alice                 │
│   │     ├─ MemPalacePythonBackend × 1 (LockedBackend)             │
│   │     │   └─ chroma.sqlite3            (单 PersistentClient)    │
│   │     └─ LockedKnowledgeGraph                                    │
│   │         └─ knowledge_graph.sqlite3   (bi-temporal triples)     │
│   │                                                                │
│   ├─ eidolon-memory-agent --user-id=bob --port=8031   …            │
│   └─ eidolon-memory-agent --user-id=charlie --port=8032 …          │
│                                                                    │
│  palace 物理隔离: ~/eidolon/memory/mempalaces/<user_id>/          │
└────────────────────────────────────────────────────────────────────┘
                              ▲             ▲
                              │             │
        NATS JetStream (写 + KG cmd)        MCP HTTP (读 + KG admin)
                              │             │
                       任何外部消费者(本节后面说明)
```

**关键**:每份 palace 文件只被**一个进程**持有(D1 铁律,避免 chromadb 多进程 corruption)。
外部访问**必须**通过 MCP / NATS / Discovery,**不要**自己开 `mempalace.knowledge_graph.KnowledgeGraph`
或 `chromadb.PersistentClient` 去碰 palace 目录。

---

## 3. 快速启动

```bash
# 1. 安装
uv sync --extra dev

# 2. 起 NATS(任何方式都行 — 不在本仓库 scope)
nats-server -js &

# 3a. 生产形态 — supervisor 读 users.yaml,自动 spawn 每个 enabled user 的 agent
eidolon-memory-supervisor &
eidolon-memory-discovery &
# 配置改动后 SIGHUP supervisor: kill -HUP $(pgrep -f eidolon-memory-supervisor)

# 3b. 开发形态 — 单用户 ad-hoc(不走 supervisor)
eidolon-memory-agent --user-id default --port 8030 &
eidolon-memory-discovery &
```

首次启动会自动 `mempalace init` 对应 palace(lazy)。配置文件见第 8 节。
本仓库**不再提供**启动脚本——三个 console-scripts (`eidolon-memory-{supervisor,agent,discovery}`)
就是全部对外契约,直接 nohup / launchd / systemd / docker / pm2 任选。

> ⚠ **代码改动后必须重启 `eidolon-memory-agent`**(`pkill -f eidolon-memory-agent` 后再起,
> 或经 supervisor / systemd restart)。Python 长生命周期进程**不做**模块 hot-reload —
> 边跑边改源代码可能让 `sys.modules` 缓存的旧模块与磁盘上的新模块对不上,
> 表现为 MCP 工具调用突然报 `ImportError`(参见 `tests/memory/test_lazy_import_guard.py`)。

---

## 4. 集成路径一:MCP Streamable HTTP(对外主路径)

每个 agent_runner 在自己的端口暴露一个 FastMCP HTTP server。用任意 MCP client(`mcp` Python SDK、Claude IDE、自研网关)连过来。

### 4.1 连接

```python
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

URL = "http://127.0.0.1:8030/mcp"   # users.yaml 里 alice 的 port

async with streamable_http_client(URL) as (read, write, _):
    async with ClientSession(read, write) as s:
        await s.initialize()
        tools = await s.list_tools()
        result = await s.call_tool("eidolon_memory_recall_context",
                                    {"query": "我喜欢什么茶", "top_k": 5})
```

**user_id 是绑定到端口的**——不需要在每次工具调用里再传 user_id,agent_runner 启动时就锁定了。

### 4.2 鉴权(可选)

在 `config/settings.yaml` 设:
```yaml
mcp_http:
  bearer_token: ""                     # 或留空走 env
  bearer_token_env: EIDOLON_MEMORY_MCP_TOKEN
```
设了之后 client 必须发 `Authorization: Bearer <token>` 头。

### 4.3 工具清单(11 个,T1+T2+T3 全量)

| 工具 | 用途 | 主要参数 |
|------|------|---------|
| `eidolon_memory_search` | 语义向量检索 | `query`, `top_k`, 可选 `wing` / `room` |
| `eidolon_memory_recall_context` | **vector + KG 融合召回**(LiveKit 同源) | `query`, `top_k`, `voice` (LiveKit 50ms KG 预算 / non-voice 1s), `include_kg`, `include_sensitive_kg` |
| `eidolon_memory_list` | 分页列举所有 drawer | `limit`, `offset`, `include_private` |
| `eidolon_memory_status` | 当前 agent 状态(palace、wings、steward mode) | — |
| `eidolon_memory_hierarchy_snapshot` | wing→room→drawer 树 | `max_records`, `max_drawers_per_room` |
| `eidolon_memory_palace_graph` | 跨翼 tunnel room 图(可视化用) | `max_nodes`, `max_edges` |
| `eidolon_memory_kg_add_triple` | **写**一条 bi-temporal 三元组(走 NATS,2s 内 sync-feel 返回) | `subject`, `predicate`, `object`, `confidence`, `valid_from?`, `valid_to?` |
| `eidolon_memory_kg_invalidate` | **结束**一条三元组(填 `valid_to`) | `subject`, `predicate`, `object`, `ended?` |
| `eidolon_memory_kg_query_entity` | 查某实体的所有当前三元组 | `name`, `direction`(outgoing/incoming/both), `include_sensitive` |
| `eidolon_memory_kg_timeline` | 按时间线列三元组 | `entity_name?`, `since?`, `until?`, `limit`, `include_sensitive` |
| `eidolon_memory_kg_snapshot` | 截断的三元组列表 + stats(图可视化用) | `max_triples`, `current_only`, `entity?`, `include_sensitive` |
| `eidolon_memory_kg_stats` | 实体/三元组计数 + active/invalidated 拆分 | — |
| `eidolon_memory_kg_predicates` | 27 个 canonical 谓词白名单 + sensitive 子集 | — |

> KG 写工具(`kg_add_triple` / `kg_invalidate`) 内部会 publish 到 NATS,然后 polling KG 表 2s 等 worker 应用。状态 `applied` = 已落盘可读;`pending` = 已发到 JetStream,worker 滞后,**保留 request_id**,过会儿会到。

### 4.4 调 `recall_context` 的典型 response

```json
{
  "context": "知识图谱事实：\n- [KG] self 喜欢 乌龙茶（自 2026-05-19T12:44Z）\n…",
  "kg_triples": [
    {"subject":"self","predicate":"likes","object":"乌龙茶","valid_from":"…","valid_to":null}
  ],
  "records": [
    {"user_id":"alice","key":"...","value":"我刚泡了乌龙","metadata":{"wing":"Wing_Life","similarity":0.78}}
  ]
}
```

`context` 是已格式化好可以直接喂给 LLM 的字符串;`records` + `kg_triples` 是原始结构供二次处理。

### 4.5 Discovery HTTP(agent-routing)

eidolon-agent 启动时先拉取 Discovery，运行中按周期刷新；MCP 端口、NATS stream/subject
模板和可用用户列表都以 memory 返回为准。Discovery 是独立核心服务，不挂在 Admin server 上。

```bash
eidolon-memory-discovery
curl http://127.0.0.1:8020/api/discovery/agent-routing
```

响应只包含 agent 路由需要的稳定契约，不暴露 `users_yaml`、`palace_path`、`pid`、`log_path`
等运维字段。开发阶段不做 token 鉴权，`mcp_auth` 固定为 `{"type":"none"}`。

```json
{
  "version": 1,
  "generated_at": "2026-05-21T10:00:00Z",
  "nats": {
    "url": "nats://127.0.0.1:4222",
    "stream": "MEMORY_TURNS",
    "turn_subject_template": "agent.memory.conversation.turn.{user_id}",
    "cmd_subject_template": "agent.memory.cmd.{user_id}"
  },
  "users": [
    {
      "user_id": "default",
      "enabled": true,
      "mcp_http_url": "http://127.0.0.1:8030/mcp",
      "mcp_auth": {"type": "none"},
      "agent_reachable": true
    }
  ]
}
```

---

## 5. 集成路径二:NATS JetStream(对话写入热路径)

**这是写入语义记忆的唯一标准路径**——发一条 `ConversationTurnPayload`,agent_runner 的同进程 steward 会异步抽取出 fragments(向量片段) + triples(KG 事实) + privacy_actions,然后落盘。

### 5.1 Subject

每个 user 一条 subject:

```
agent.memory.conversation.turn.<user_id>
```

Stream 名(供配 publisher / replay):
```
MEMORY_TURNS
```

(JetStream subjects 配在 `eidolon/memory/infrastructure/nats_stream.py:all_stream_patterns()`,
你不需要自己管 stream creation——agent_runner 启动时会 ensure 。)

### 5.2 Payload schema

```json
{
  "turn_id":       "uuid4",                // 唯一,用于 G1 idempotency
  "user_id":       "alice",                // 必须匹配端口绑定的 user
  "session_id":    "session-abc",
  "timestamp":     "2026-05-19T10:00:00Z", // ISO8601 UTC
  "user_text":     "我妈最近失眠",
  "assistant_text":"听起来你很担心她",
  "metadata":      { "source": "livekit", "...": "..." }   // 可选
}
```

### 5.3 发送示例 (`nats-py`)

```python
import json, uuid
from datetime import datetime, timezone
import nats

nc = await nats.connect("nats://127.0.0.1:4222")
js = nc.jetstream()

await js.publish(
    "agent.memory.conversation.turn.alice",
    json.dumps({
        "turn_id":        uuid.uuid4().hex,
        "user_id":        "alice",
        "session_id":     "s1",
        "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_text":      "我妈最近失眠",
        "assistant_text": "听起来你很担心她",
    }).encode(),
)
```

发出去就走人,**不会阻塞对话**。worker 内 steward 抽取 + 同步写 chroma fragment + 同步写 KG triple,JetStream 是事实源(D5:14 天历史足以 replay 重建 palace)。

### 5.4 命令路径(admin 写 KG)

Admin / 外部工具想直接写 KG 三元组(绕过 steward 抽取),走另一条 subject:

```
agent.memory.cmd.<user_id>
```

Payload 是 `KgAddTripleCommand` / `KgInvalidateCommand`(见 `eidolon/memory/domain/kg.py`)。
推荐用 MCP 工具 `eidolon_memory_kg_add_triple` 间接发——它已经把 publish + 2s polling 包好了。

---

## 6. 集成路径三:同进程 Python API(LiveKit / monorepo)

当你和 agent_runner 在**同一个 Python 进程**里(典型:LiveKit pipeline 把记忆服务 embed 进自己的进程),直接 import 比 HTTP 省 5–20ms framing。

### 6.1 LiveKit hot-path recall

```python
from eidolon.memory.application.livekit_recall import LiveKitRecallService

svc = LiveKitRecallService(
    backend=locked_backend,         # 同 agent_runner 持有的 LockedBackend 实例
    settings=memory_settings,
    palace_path="/Users/.../mempalaces/alice",
    kg=locked_kg,                   # 可选,传则启用 KG 融合
)
result = await svc.recall_context_with_records(
    query="我妈最近怎么样",
    user_id="alice",
    session_id="livekit-s1",
)
# result["context"]   → 喂给 LLM 的字符串(含 KG 转录)
# result["degraded"]  → 300ms 超时退化标志
```

`recall_context_with_records` 内部已经 `asyncio.wait_for(timeout=settings.recall.livekit_timeout_seconds)`(默认 300ms),**永不抛**——超时/错误返回 `degraded: true` + 空 context。

### 6.2 直接喂 MemoryFragment(绕过 steward)

```python
from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.domain.fragments import MemoryFragment

await ingest_memory_fragment(locked_backend, MemoryFragment(
    fragment_id="f-...",
    user_id="alice",
    wing="Wing_Profile", room="profile_core",
    content="用户喜欢乌龙茶",
    memory_type="preference", importance=4, confidence=0.95,
    source_turn_id="t-...", session_id="s1",
))
```

仅限同进程,**外部进程千万不要**这么干(会绕开 D1 single-owner 不变量)。

---

## 7. 多用户 / 进程管理

### 7.1 users.yaml

每个 user 一条记录,声明端口和 enabled:

```yaml
# 默认路径: config/users.yaml (init 从 config/users.yaml.tpl 复制)
# (可由 settings.supervisor.users_file 或 EIDOLON_MEMORY_USERS_YAML 覆盖)
users:
  - id: alice
    port: 8030
    enabled: true
  - id: bob
    port: 8031
    enabled: true
  - id: charlie
    port: 8032
    enabled: false      # 关停 — palace 数据保留,翻 true 后即恢复
```

约束(`UsersConfig` pydantic 验证):
- `id` 唯一
- `port` 在 enabled 之间不重复

### 7.2 Supervisor(纯 Python,不是 supervisord)

```bash
eidolon-memory-supervisor       # 前台
```

行为:
- 读 users.yaml,对每个 enabled user `subprocess.Popen` 起 `eidolon-memory-agent`
- 5s poll 检查死掉的子进程,按 `[1, 2, 4, 8, 30]` s 退避重启,60s 内连续 5 次失败标记 degraded
- `SIGHUP` → 重读 users.yaml,新增 user spawn / 删除 SIGTERM
- `SIGTERM` → 给每个子进程 30s grace,超时 SIGKILL

**不依赖 launchd / systemd / supervisord** — 自己一份 ~400 行 Python。

### 7.3 单用户 ad-hoc(开发)

```bash
eidolon-memory-agent --user-id default --port 8030
```

完全独立于 supervisor;两者可以混跑(每个 palace 仍只一份进程持有)。

### 7.4 用户增删改

直接编辑 `users.yaml`,然后:
```bash
kill -HUP $(pgrep -f eidolon-memory-supervisor)
```
supervisor 收到 SIGHUP 会重读 yaml:新增 `enabled: true` 的行 → 自动 init palace
+ spawn agent;现有 user 切到 `enabled: false` → SIGTERM 该 agent(palace 数据保留)。

如需脚本化批量管理,直接调 `eidolon.memory.config.users_io` 模块的
`upsert_user` / `update_enabled` / `remove_user`(fcntl flock 跨进程安全)。

---

## 8. 配置

### 8.1 主配置文件

```bash
# 优先级:
# 1. $EIDOLON_MEMORY_SETTINGS_YAML
# 2. config/settings.yaml (gitignored; init 从 config/settings.example.yaml 复制)
```

关键字段(完整字段见 `config/settings.example.yaml`):

```yaml
nats:
  url: "nats://127.0.0.1:4222"
  stream: "MEMORY_TURNS"

mcp_http:
  host: "127.0.0.1"
  port: 8030                     # 仅用于 ad-hoc 单用户;多用户走 users.yaml
  path: "/mcp"
  bearer_token: ""               # 启用后 client 必须发 Authorization
  bearer_token_env: EIDOLON_MEMORY_MCP_TOKEN

discovery_http:
  host: "127.0.0.1"
  port: 8020
  path: "/api/discovery/agent-routing"

steward:
  mode: "llm"                    # llm | rule | noop

llm:
  model: "openai/local-model"
  base_url: "http://127.0.0.1:1234/v1"
  api_key: ""
  api_key_env: EIDOLON_MEMORY_LLM_API_KEY

recall:
  livekit_timeout_seconds: 0.3   # LiveKit 整体 wait_for
  kg_in_recall: true             # 默认启用 KG 融合
  kg_timeout_seconds: 0.05       # voice 路径 KG 子超时
  top_k: 5

kg:
  min_confidence_to_write: 0.6   # steward 输出低于此置信的 triple 丢弃 (G10)

chromadb:
  synchronous: FULL              # D3 hard-kill 持久性

supervisor:
  users_file: "config/users.yaml"
  eager_init: true
```

### 8.2 环境变量

| 变量 | 用途 |
|------|------|
| `EIDOLON_MEMORY_SETTINGS_YAML` | 主配置文件路径 |
| `EIDOLON_MEMORY_USERS_YAML` | users.yaml 路径(覆盖 supervisor.users_file) |
| `EIDOLON_MEMORY_PALACES_ROOT` | per-user palace 目录的父根 |
| `EIDOLON_MEMORY_MCP_TOKEN` | MCP HTTP bearer token |
| `EIDOLON_MEMORY_LLM_API_KEY` | steward LLM 密钥 |

### 8.3 Palace 目录布局

```
~/eidolon/memory/mempalaces/<user_id>/
  ├─ chroma.sqlite3              # 向量 + 元数据 (chromadb, WAL)
  ├─ chroma.sqlite3-wal
  ├─ knowledge_graph.sqlite3     # bi-temporal KG (mempalace.KnowledgeGraph)
  ├─ knowledge_graph.sqlite3-wal
  └─ mempalace.yaml              # mempalace 自身配置
```

**绝不要把 palace 目录放在 iCloud / Dropbox / OneDrive / NFS** — 启动时会拒绝。

---

## 9. 没有 Admin HTTP API

本项目不再提供 Admin/UI 层。所有运维/调试能力(写 turn / 写 KG / 读召回 /
列 MCP 工具 / 列 users.yaml)都通过 **MCP 工具**(§4)和 **NATS subject**(§5)
直接暴露,任何外部 gateway / 网关 / CLI 都可以照样消费,**不需要中间层**。

需要自定义网关的话,从 `eidolon.memory.config.users_io` + MCP HTTP +
`JetStreamTurnPublisher` 几块乐高直接拼,参考 `tests/memory/test_kg_*.py` 的用法。

---

## 10. 运维 / 灾备

### 10.1 完整性检查

agent_runner 启动时跑 `PRAGMA integrity_check` on 两个 SQLite,失败则**不订阅 NATS / 不监听端口**,需要运维介入。

### 10.2 快照

```bash
scripts/snapshot_palaces.sh
# 每 6h 跑(launchd / cron),tar.zst 全部 palace 到 ~/eidolon/snapshots/
# 保留 24 份(6 天)
```

### 10.3 从 JetStream 重建 palace(D5)

```bash
scripts/rebuild_palace_from_jetstream.py --user-id alice
# 从 stream 头部 replay 全部 ConversationTurnPayload + Kg*Command
# drawer_id = sha256(...) 保证 replay 幂等
# JetStream 14 天历史 = RPO 14 天
```

KG 写也走 JetStream(`agent.memory.cmd.*`),所以 admin 写过的三元组**也能 replay 回来**。

---

## 11. 测试

```bash
# 单元 + 集成 (T1+T2+T3 + 跨层闭环):
uv run pytest tests -q                           # 145 passed, 2 skipped

# 性能基线 (300ms SLA):
.venv/bin/python scripts/benchmark/bench_read_livekit.py
.venv/bin/python scripts/benchmark/bench_read_livekit.py --with-kg
```

`tests/memory/test_kg_fusion_integration.py` 是跨 T1/T2/T3 的闭环用例(对话 → steward → KG → 召回),
作为外部集成方的**可执行规约**参考。

---

## 12. 不在范围(下一计划)

- 跨用户共享记忆 — D1 物理隔离,陪伴场景永远不该跨用户(Alice 的 AI 不能知道 Bob 的事)
- KG 清理 / consolidation — 现在 invalidate 只写 `valid_to` 不删行,3-5 年陪伴单用户量级毫无压力
- Hybrid 召回(BM25+dense+RRF)、reranker、entity 规范化进化 — 见 `docs/plan-kg-integration.md`
- 多模态 fragments(图像/音频片段) — 当前只有文本

---

## 13. 代码地图(供深读)

| 层级 | 目录 | 角色 |
|------|------|------|
| Domain | `eidolon/memory/domain/` | `MemoryFragment` / `ConversationTurnPayload` / `Kg*` schema, `MemoryBackend` 端口 |
| Config | `eidolon/memory/config/` | `MemorySettings`, `UsersConfig`, palace 解析, steward prompts |
| Infrastructure | `eidolon/memory/infrastructure/` | NATS / JetStream / 完整性 / palace init |
| Adapters | `eidolon/memory/adapters/` | `MemPalacePythonBackend`, `LockedBackend`, `LockedKnowledgeGraph`, `FakeMemoryBackend` |
| Application | `eidolon/memory/application/` | `turn_processor`, `livekit_recall`, `public_recall` (融合), `kg_recall`, steward |
| Entrypoints | `eidolon/memory/entrypoints/` | `agent_runner`(主进程)、`supervisor`、`mcp_server`(工具注册)、`discovery_server` |

---

## 14. License

MIT(见 `pyproject.toml`)。
