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

## 2. 架构总览

### 2.1 部署拓扑(进程 / 端口 / 文件)

```
┌───────────────────────────────────────────────────────────────────┐
│ eidolon-memory-supervisor  (Python,纯进程经理,subprocess.Popen)  │
│   │  读 eidolon_admin registry,per-user fan-out;SIGHUP reconcile │
│   ├─ eidolon-memory-agent --user-id=alice --port=8030 ────────────┤
│   │     ├─ LiveKit pipeline (in-process recall)                    │
│   │     ├─ MCP Streamable HTTP @ 127.0.0.1:8030/mcp               │
│   │     ├─ NATS subscriber  agent.memory.conversation.turn.alice   │
│   │     │                   agent.memory.cmd.alice                 │
│   │     ├─ MemPalacePythonBackend × 1 (LockedBackend)             │
│   │     │   ├─ chroma.sqlite3            (单 PersistentClient)    │
│   │     │   └─ WorkingMemoryRing         (in-memory,共享 lock)    │
│   │     └─ LockedKnowledgeGraph                                    │
│   │         └─ knowledge_graph.sqlite3   (triples + entity_mentions)│
│   │                                                                │
│   ├─ (opt-in) eidolon-memory-consolidator --user-id=alice ─────────┤  Phase 4
│   │     主题摘要 worker:MCP 读 drawers → LLM → NATS cmd 写主题     │
│   │                                                                │
│   ├─ eidolon-memory-agent --user-id=bob --port=8031   …            │
│   └─ eidolon-memory-agent --user-id=charlie --port=8032 …          │
│                                                                    │
│  palace 物理隔离: ~/eidolon/palaces/<user_id>/                    │
└────────────────────────────────────────────────────────────────────┘
                              ▲             ▲
                              │             │
        NATS JetStream (写 + KG cmd)        MCP HTTP (读 + KG admin)
                              │             │
                       任何外部消费者(本节后面说明)
```

**D1 铁律**:每份 palace 文件只被**一个进程**持有(避免 chromadb 多进程 corruption)。
外部访问**必须**通过 MCP / NATS / Discovery,**不要**自己开 `KnowledgeGraph` /
`chromadb.PersistentClient` 去碰 palace 目录。consolidator 也遵守此律——它是"另一个
客户端"(MCP 读 + NATS 写),不持 chroma 句柄。

### 2.2 分层架构(DDD,7 个包)

依赖**单向向下**,下层不 import 上层:

```
entrypoints/   进程入口 · CLI · 进程经理
  supervisor.py        多用户 fan-out(agent + 可选 consolidator)+ 重连/重启
  agent_runner.py      单用户进程:MCP server + NATS subscriber(带重连韧性)
  consolidator.py      主题 worker(独立进程)
  mcp_server.py        FastMCP 工具注册(14 个工具)
  discovery_server.py  agent-routing HTTP
        │ 调用
        ▼
application/   用例编排(无 IO 细节,只编排)
  turn_processor.py    写路径:turn → steward → fragments/triples/mentions
                       cmd 路径:kg_add / kg_invalidate / theme / user_confirm
  public_recall.py     读路径:recall_with_kg_fusion(融合中枢)
  recall_rerank.py     BM25 + cosine RRF rerank            (Phase 1)
  recall_renderer.py   渲染 [最近对话]/[主题]/wing 分组/[知识图谱事实]
  working_memory.py    WorkingMemoryRing 短期对话环          (Phase 2)
  kg_recall.py         KG 实体路由 + triple 转中文叙述
  steward/             noop | rules | llm(factory 选择)
  livekit_recall.py    voice 300ms 预算封装
        │ 依赖抽象(Protocol)
        ▼
domain/        纯数据 + 契约(pydantic,零 IO)
  ports.py             MemoryReader/Writer/Backend Protocol(lock + working_memory)
  wire.py / fragments.py / payloads.py / kg.py / steward.py / wings.py
        ▲ 被实现
        │
adapters/      具体 IO 实现
  locked_backend.py    asyncio.Lock 包 chromadb(D1 单写单读)
  locked_kg.py         asyncio.Lock 包 KG sqlite + entity_mentions(Phase 3)
  mempalace_python_backend.py  真 chromadb;search_payload 解析
  fake_backend.py      内存假实现(单测)
infrastructure/  NATS / stream / checkpoint / palace init / CPU 调优
config/          settings.yaml + .env 加载
support/         logging / pydantic base
```

**解耦关键**:`application/` 只依赖 `domain/ports.py` 的 Protocol(`backend.lock`、
`backend.working_memory`),从不 import 具体的 `LockedBackend`——所以测试可以塞
`FakeMemoryBackend`,生产塞 `MemPalacePythonBackend`,召回逻辑一行不改。

### 2.3 写路径 — 两条 NATS subject,8 个逻辑分支

```
NATS JetStream
 ├─ agent.memory.conversation.turn.<uid>   (对话热路径)
 │     → agent_runner._nats_subscriber_loop._drain
 │     → turn_processor.process_turn_message
 │        1. JSON 解析失败            → ack 丢弃(不 NAK)
 │        2. user_id 不匹配           → ack 丢弃
 │        3. ★ working_memory.append(turn)  ← 先于 steward(G7:连续性≠抽取质量)
 │        4. steward.decide(turn):
 │             ├─ noop  → 空 decision(只 ack)
 │             ├─ rules → 正则抽 1 fragment + privacy_actions
 │             └─ llm   → LiteLLM 抽 fragments+triples+invalidations+mentions
 │                         (失败 → fallback rules)
 │        5. fragment 写失败          → NAK / 超 max_deliveries 进 DLQ
 │        6. KG triple/invalidation 写 → 失败仅 log(G7,不 NAK)
 │        7. ★ mentions 写入(Phase 3):entity_id 必须在本 turn triples 里
 │             → 否则拒绝(防 LLM 幻觉)
 │        8. ack
 │
 └─ agent.memory.cmd.<uid>                 (admin / 系统写,绕 steward)
       → turn_processor.process_command_message,按 kind 分发:
          ├─ kg_add_triple              → LockedKG.add_triple
          ├─ kg_invalidate              → LockedKG.invalidate
          ├─ consolidator_ingest_theme  → 直写 Wing_Theme fragment   (Phase 4)
          └─ memory_intent              → 显式意图直写 drawer/KG
```

**NATS subscriber 韧性**(重构):`_drain` 把 fetch + handler 都包进重连边界;长 LLM
ingestion 期间连接被 drain → `ack()` 抛错 → **外层 reconnect-and-resubscribe 循环**
(指数退避)而非永久死亡。turn/cmd 各自吞 idle TimeoutError,互不饿死。

### 2.4 读路径 — recall 融合中枢,7 个信号源叠加

`application/public_recall.py::recall_with_kg_fusion` 是所有读的中枢。**并行**拉 vector
+ KG,再叠加 working memory / 主题 / user-confirmed,最后渲染:

```
recall_context(query, voice?)
 │
 ├─[A] vector_task  search_all_wings_mcp_style
 │       ├─ voice=True 且 shared_query_embedding → 单次 ONNX embed + 各 wing 并行 query(快路径)
 │       └─ voice=False → 各 wing backend.search 扇出 + rank_by_similarity 截 top_k
 │       fan-out 排除 Wing_Privacy + Wing_Theme(Phase 4.1:主题不抢具体事实的 top_k)
 │
 ├─[B] kg_task(可选,settings.recall.kg_in_recall)
 │       match_entities_for_query:① 字面 ② 前缀剥离(pet:铁锤→铁锤)
 │                                 ③ alias 反查(我妈→mother:张丽,Phase 3)
 │       → query_entity_combined(SQL ORDER BY confidence DESC,Phase 1)
 │       voice 超时 50ms / 非 voice 1s,超时静默退化为 vector-only
 │
 ├─[C] rerank(settings.recall.rerank_enabled,Phase 1)
 │       BM25 + cosine RRF 融合;rank-bm25 缺失/抛错 → 恒等退化
 │
 ├─[D] user-confirmed 置顶(Phase 5.2)
 │       _is_user_confirmed:metadata.source 或 room 前缀 `userconfirm:`
 │       (mempalace search 丢 metadata,room 是唯一存活信号)→ 拉到 vector 最前
 │
 ├─[E] 主题独立通道(Phase 4 + 4.1)
 │       _fetch_themes:单独 search Wing_Theme,theme_top_k 上限
 │       + theme_min_similarity=0.55 相关性门槛(低于则丢,防越界泄漏)
 │
 ├─[F] working memory 快照(Phase 2)
 │       backend.working_memory.snapshot() → 最近 N turn verbatim
 │
 └─[G] 渲染 recall_renderer.group_recall_context,段顺序:
         [最近对话] → [主题] → wing 分组(个人画像/情绪/工作…) → [知识图谱事实]
```

返回 `{context: str, records: [...], kg_triples: [...], working_memory: [...]}`。
任一信号源失败都不打断 recall(全程 defensive,voice 300ms 预算硬保)。

### 2.5 形态演进:查询式 → 情境式(本轮重构的全部内容)

原系统是**形态 1 查询式**(agent 主动 query 才有数据)。本轮把它推进到**形态 2 情境式**
(memory 主动注入连续性 + 主题 + 关系别名 + 用户确认),分阶段、每阶段独立验证:

| 阶段 | 解决的痛点 | 核心机制 | 落点 | tag |
|------|-----------|---------|------|-----|
| **P0** | 长进程 lazy import 撞 stale 模块 | 跨包 import 上提 + AST 守门 | `test_lazy_import_guard` | — |
| **P1** | cosine 噪声压过关键词命中 | BM25+cosine RRF rerank + KG confidence 排序 | `recall_rerank.py` | — |
| **P2** | "刚才说啥"无法 cosine 答 | 内存环 + `[最近对话]` 段 | `working_memory.py` | `phase2-complete` |
| **P3** | "我妈/我家狗"别名命中不了 canonical | `entity_mentions` 表 + alias 反查 + steward 防幻觉 | `locked_kg.py` | `phase3-complete` |
| **P4** | "最近怎样"只给零散 fragment | consolidator 独立进程 + Wing_Theme + `[主题]` | `consolidator.py` | `phase4-complete` |
| **P4.1** | 主题挤占具体事实(精度代价 −15~−20pp) | 主题移出竞争 top_k + 相关性门槛 | `public_recall.py` | `phase4.1-complete` |
| **P5.2** | 用户说"记住X"被 steward 改写/丢 | cmd 直写通道 + recall 置顶 | `turn_processor.py` | `phase5.2-complete` |

每阶段都过 **U/F/E/P/R 五闸门**(单元/功能/e2e/性能/回归);Phase 4 还经 `--with-consolidator`
A/B bench 量化(靶向类目 emotion/time/topic +10~+33pp,精度类目零回归)。
完整执行记录见 `~/.claude/plans/mempalace-mcp-nats-admin-robust-meadow.md` 的「执行记录」节。

**重构期间挖出并修复的 3 个潜伏生产 bug**(详见 §13.1):NATS subscriber 永久死循环、
WAL checkpoint 的 `Path` NameError、stored `source` metadata 三处覆盖。

---

## 3. 快速启动

```bash
# 1. 安装
uv sync --extra dev

# 2. 起 NATS(任何方式都行 — 不在本仓库 scope)
nats-server -js &

# 3a. 生产形态 — supervisor 读 eidolon_admin registry,自动 spawn 每个 enabled user 的 agent
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

URL = "http://127.0.0.1:8030/mcp"   # admin registry 里 alice 的 memory_port

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
  bearer_token_env: EIDOLON_MEMORY_MCP_TOKEN  # 值在 config/.env
```
设了之后 client 必须发 `Authorization: Bearer <token>` 头。

### 4.3 工具清单(14 个,T1+T2+T3 + 形态 2 全量)

| 工具 | 用途 | 主要参数 |
|------|------|---------|
| `eidolon_memory_search` | 语义向量检索 | `query`, `top_k`, 可选 `wing` / `room` |
| `eidolon_memory_recall_context` | **vector + KG + 主题 + 工作记忆 融合召回**(LiveKit 同源) | `query`, `top_k`, `voice` (LiveKit 50ms KG 预算 / non-voice 1s), `include_kg`, `include_sensitive_kg` |
| `eidolon_memory_user_confirm` | **用户确认意图**(绕 steward、经 NATS 单写 worker 投影、verbatim、召回置顶) | `text`, `wing`, `memory_type`, `importance`, `confidence`, `tags`, `source_event_id`, `tool_call_id` |
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
    {
      "user_id":"alice",
      "key":"...",
      "value":"我刚泡了乌龙",
      "memory_time":"2026-05-19T12:40:00Z",
      "memory_time_source":"occurred_at",
      "created_at":"2026-05-19T12:40:02Z",
      "metadata":{"wing":"Wing_Life","similarity":0.78}
    }
  ]
}
```

`context` 是已格式化好可以直接喂给 LLM 的字符串;向量记忆行会在可用时渲染
`[YYYY-MM-DD]` 前缀。`records[].memory_time` 是上层统一消费的记忆时间点,
优先级为 `occurred_at > valid_from > created_at > filed_at > updated_at`;
`memory_time_source` 表明它来自哪个原始字段。`records` + `kg_triples` 是原始结构供二次处理。

### 4.5 Discovery HTTP(agent-routing)

eidolon-agent 启动时先拉取 Discovery，运行中按周期刷新；MCP 端口、NATS stream/subject
模板和可用用户列表都以 memory 返回为准。Discovery 是独立核心服务，不挂在 Admin server 上。

```bash
eidolon-memory-discovery
curl http://127.0.0.1:8020/api/discovery/agent-routing
```

响应只包含 agent 路由需要的稳定契约，不暴露 `palace_path`、`pid`、`log_path`
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

### 7.1 用户 / 主权数据入口

用户、companion、设备授权等主权数据由 `eidolon_data` 统一管理。默认本地
SQLite 路径为 `~/eidolon/data/eidolon.sqlite3`，可通过
`EIDOLON_DATA_SQLITE_PATH` 覆盖。

Memory 作为记忆引擎不拥有用户注册表。跨进程读取时消费 admin / data 的只读
视图；同进程组合时由运行时注入 `DataStore(memory_engine=...)`。

```text
GET http://127.0.0.1:9000/api/users/registry
```

每个 user 的运行态字段包括 `user_id`、`enabled`、`memory_port`、`palace_path`
和 consolidator 配置。新增、启停、端口与 consolidator 配置变更都通过
`eidolon_admin /api/users` 完成。

### 7.2 Supervisor(纯 Python,不是 supervisord)

```bash
eidolon-memory-supervisor       # 前台
```

行为:
- 读 admin registry,对每个 enabled user `subprocess.Popen` 起 `eidolon-memory-agent`
- 5s poll 检查死掉的子进程,按 `[1, 2, 4, 8, 30]` s 退避重启,60s 内连续 5 次失败标记 degraded
- `SIGHUP` → 重读 admin registry,新增 user spawn / 删除 SIGTERM
- `SIGTERM` → 给每个子进程 30s grace,超时 SIGKILL

**不依赖 launchd / systemd / supervisord** — 自己一份 ~400 行 Python。

### 7.3 单用户 ad-hoc(开发)

```bash
eidolon-memory-agent --user-id default --port 8030
```

完全独立于 supervisor;两者可以混跑(每个 palace 仍只一份进程持有)。

### 7.4 用户增删改

通过 `eidolon_admin /api/users` 管理用户。Admin 写入统一 registry DB 后会触发
memory supervisor reconcile；也可以手动发送:

```bash
kill -HUP $(pgrep -f eidolon-memory-supervisor)
```

supervisor 收到 SIGHUP 会重读 admin registry:新增 `enabled=true` 的用户 →
自动 init palace + spawn agent;现有 user 切到 `enabled=false` → SIGTERM 该
agent(palace 数据保留)。

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
  port: 8030                     # 仅用于 ad-hoc 单用户;多用户走 admin registry
  path: "/mcp"
  bearer_token_env: EIDOLON_MEMORY_MCP_TOKEN  # 值在 config/.env

discovery_http:
  host: "127.0.0.1"
  port: 8020
  path: "/api/discovery/agent-routing"

steward:
  mode: "llm"                    # llm | rule | noop

llm:
  model: "openai/local-model"
  base_url: "http://127.0.0.1:1234/v1"
  api_key_env: EIDOLON_MEMORY_LLM_API_KEY  # 值在 config/.env

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
  admin_api_url: "http://127.0.0.1:9000"
  eager_init: true
```

### 8.2 环境变量

| 变量 | 用途 |
|------|------|
| `EIDOLON_MEMORY_SETTINGS_YAML` | 主配置文件路径 |
| `EIDOLON_ADMIN_API_URL` | admin registry API base URL |
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
列 MCP 工具 / memory routing discovery)都通过 **MCP 工具**(§4)和 **NATS subject**(§5)
直接暴露,任何外部 gateway / 网关 / CLI 都可以照样消费,**不需要中间层**。

需要自定义网关的话,从 admin registry API + MCP HTTP + `JetStreamTurnPublisher`
几块乐高直接拼,参考 `tests/memory/test_kg_*.py` 的用法。

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
| Domain | `eidolon/memory/domain/` | `MemoryFragment` / `ConversationTurnPayload` / `Kg*` schema, `EntityMention`, `ports.py`(`MemoryBackend` Protocol + `lock`/`working_memory`) |
| Config | `eidolon/memory/config/` | `MemorySettings`(含 recall.rerank/theme 旋钮)、`UsersConfig`(含 `consolidator`)、palace 解析、steward prompts |
| Infrastructure | `eidolon/memory/infrastructure/` | NATS / JetStream stream / WAL checkpoint / 完整性 / palace init |
| Adapters | `eidolon/memory/adapters/` | `MemPalacePythonBackend`、`LockedBackend`、`LockedKnowledgeGraph`(+`entity_mentions`)、`search_payload`、`FakeMemoryBackend` |
| Application | `eidolon/memory/application/` | `turn_processor`(写)、`public_recall`(读融合)、`recall_rerank`(P1)、`recall_renderer`、`working_memory`(P2)、`kg_recall`、`steward/*` |
| Entrypoints | `eidolon/memory/entrypoints/` | `agent_runner`(主进程 + NATS 重连)、`supervisor`(fan-out)、`consolidator`(P4)、`mcp_server`(14 工具)、`discovery_server` |
| Bench | `scripts/benchmark/` | `bench_read_livekit`(R-01 延时)、`bench_memory_retrieve_quality`(`--with-consolidator` A/B 质量) |

### 13.1 重构期间发现并修复的 3 个潜伏生产 bug

端到端真实验证(长 LLM ingestion + 真 NATS)逼出的、单测 mock 不掉的问题:

1. **NATS subscriber 遇瞬时断连永久死亡**(`agent_runner.py`)
   `_drain` 只把 `psub.fetch` 包 try,`msg.ack()` 在外;长 ingestion 期连接被 drain →
   ack 抛错 → 逃出 while → 永久停止消费 turn+cmd。**修**:外层 reconnect-and-resubscribe
   循环(指数退避),turn/cmd 各自吞 idle TimeoutError。

2. **WAL checkpoint 的 `Path` NameError**(`agent_runner.py`)
   `Path` 只在 `main()` 内 import,`_nats_subscriber_loop` 取不到 → 每 5 turn checkpoint
   时 NameError(被 bug 1 掩盖)→ ingestion 期 WAL checkpoint 实际从未运行。**修**:
   `from pathlib import Path` 提模块顶层。

3. **stored `source` metadata 在 3 处读路径被覆盖**(`mempalace_python_backend` /
   `search_payload` / `fake_backend`)
   get_all→`mempalace-python`、search→`mcp`、fake→`fake`;且 mempalace 向量 search 只回
   `{text,wing,room,source_file,similarity}`(自定义 metadata 全丢)。导致 P5.2 user-confirmed
   置顶 + P4 source 检查静默失效。**修**:三处改"仅缺失时默认";user-confirmed 改用
   `room` 前缀(search 会保留)做信号。

### 13.2 关键不变量(改代码前必读)

- **D1 单写单读**:一份 palace 一个进程;`LockedBackend`/`LockedKnowledgeGraph` 共享同一把
  `asyncio.Lock`。consolidator 不持 chroma 句柄(它是 MCP 读 + NATS 写的"另一个客户端")。
- **召回热路径只能依赖 mempalace search 保证返回的字段**:`{text, wing, room, source_file,
  similarity}`。任何依赖自定义 metadata 的召回逻辑都会失效(见 13.1#3)——用 `room` 前缀或
  专属 wing 做信号。
- **G7 写顺序**:turn 先 append working_memory 再跑 steward——短期连续性独立于抽取质量。
- **NATS-write / MCP-read 契约**:所有 e2e 必须走真链路(NATS 发、MCP 读),禁止 in-process
  直调 backend/kg 作捷径。
- **代码改后必重启 agent**(无 hot-reload,见 §3 警告 + `test_lazy_import_guard`)。
- **零成本回滚旋钮**:`recall.rerank_enabled`、`runtime.working_memory_maxlen=0`、
  `recall.theme_top_k=0`、`recall.kg_in_recall` —— 每个新形态都能配置关掉退回旧行为。

---

## 14. License

MIT(见 `pyproject.toml`)。
