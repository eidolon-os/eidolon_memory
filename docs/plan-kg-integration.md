# Plan：mempalace KnowledgeGraph 接入（陪伴场景）

> 范围：把 mempalace 自带的 `KnowledgeGraph` 类（temporal SQLite KG，对标 Zep/Graphiti）
> 接入 D1 per-user agent_runner，提供 entity-first / 时态感知 / 矛盾消解能力。
>
> 不在范围：自造图引擎、entity NER 全自动 pipeline（用 mempalace `entity_detector`
> 时谨慎，留 Tier 3）、跨用户 KG 共享、可视化界面。

## 架构铁律（v1.1 锁定）

1. **admin 是受约束的 agent 客户端**——所有写入（fragment、triple、invalidate、删除）**必须经 NATS JetStream**，不允许 MCP 工具直写 backend / KG。
   - JetStream 是 single source of truth（D5 灾难恢复依赖）
   - admin 直写 → replay 时丢数据 + 写入路径分裂 + 锁模型复杂化
   - 唯一例外：**读工具**直接走 LockedBackend（无副作用）
2. **效率与体验是不可让步的硬约束**
   - LiveKit recall 仍 ≤ 300ms 端到端（hard）
   - admin 写**视觉延迟** ≤ 200ms（sync-feel UX：publish + 短轮询 KG 直到可见）
   - KG 永远不能拖垮 LiveKit——双重护栏：内部 50ms 超时 + 外层 300ms wait_for
   - 所有 KG 写失败**不阻断 turn ack**（fragment 已成功，KG 是增量）

---

## 0. Why 接入 KG（最短论证）

LiveKit 陪伴场景里，单纯 dense embedding 召回有三个无解题：

1. **改变心意** — 用户去年讨厌咖啡今年喜欢，向量库会同时召出"讨厌"与"喜欢"两条
   drawer，对话 LLM 自己消化得不好就翻车。
2. **承诺/兑现** — "我答应妈妈周末回家" 这种带过期/状态的事项，向量召回拿不到结构。
3. **实体集中查询** — "我妈最近怎么样" 应该返回**关于妈妈的所有当前有效事实**，
   不是命中"提到妈妈那条 drawer"。

`mempalace.knowledge_graph.KnowledgeGraph` 已经提供：

- bi-temporal 三元组（`valid_from` / `valid_to`），`as_of` 时点查询
- `invalidate(s, p, o, ended)` 显式停效，保留历史
- 本地 SQLite，单 writer + WAL，与 D1 进程独占模型契合
- 不需 Neo4j，不需要订阅费

→ 这套是被 mempalace 已经写好的轮子，**接入而非自造**。

---

## 1. 三个 Tier 的总览

| Tier | 内容 | 工作量 | 交付价值 |
|------|------|--------|---------|
| **T1 接入** | per-user KG 文件、MCP 工具集、手动写入 | **2 天** | 你/Admin/Claude IDE 立刻能玩；smoke 验证语义 |
| **T2 自动写** | Steward 扩展输出 triples / invalidations；worker 写入 | **3-4 天** | 对话自动建图；改变心意自动处理 |
| **T3 智能召回** | `recall_context` 内置 routing；entity 别名表；KG triples 转自然语句拼 context | **3-5 天** | LiveKit 热路径享受 KG，无侵入 |

**本 plan 完整覆盖 T1+T2+T3**（8-11 天，可按 commit 分批）。

---

## 2. 架构定位

```
agent_runner (per user)
  ├── MemPalacePythonBackend                    (chroma drawers, 现有)
  ├── KnowledgeGraph(<palace>/knowledge_graph.sqlite3)   (新)
  ├── LockedBackend  ←──┐
  │   ├── search/ingest_text/ingest_fragment    (代理 MemPalace)
  │   └── kg_add / kg_invalidate / kg_query     (代理 KG)        共享一把 asyncio.Lock
  └── Steward                                              ────┘
       extract → StewardDecision(fragments, triples, invalidations, ...)

ASIDE: 共享锁的理由 — KG 和 chroma 是不同 SQLite 文件，但同一进程内并发可能
踩 GIL 之外的资源（fs cache、Python sqlite3 全局状态）。陪伴单用户写量极低，
锁竞争可忽略；保守起见走"一把大锁"哲学。如果 T3 上线后实测锁等待 P95 > 5ms 才考虑拆。
```

### 2.1 文件布局

```
~/eidolon/palaces/<user_id>/
  ├── chroma.sqlite3
  ├── <collection-uuid>/...
  ├── mempalace.yaml
  └── knowledge_graph.sqlite3      ← NEW (mempalace KnowledgeGraph)
```

`snapshot_palaces.sh` 已经 `tar.zst` 整个 palace 目录，KG 文件**自动备份**。
`rebuild_palace_from_jetstream.py` replay 时 KG 也会被 steward 自动重建——
JetStream 14 天对话历史保持单一 source of truth。

### 2.2 进程边界

KG 实例与 chromadb PersistentClient 一样，**只在 agent_runner 进程内**存活。
Admin / IDE 不能直接打开 KG 文件（避免多进程 SQLite 撕裂），必须经
control-plane MCP 工具。

---

## 3. Tier 1：接入 + 通过 NATS 手动写入

### 3.0 LockedKnowledgeGraph 幂等性（G1 修补）

mempalace `KnowledgeGraph.add_triple()` 内部用 `uuid4()` 生成主键 → **同一 (s,p,o,source) replay 会产生重复 triple**（NAK 重投或 rebuild 都会触发）。这与 chroma drawer 那边 `fragment_id = sha256(...)` 的幂等设计**必须对齐**。

我们在 `LockedKnowledgeGraph` 包装层做 deterministic ID + INSERT OR IGNORE：

```python
class LockedKnowledgeGraph:
    """asyncio.Lock-shared wrapper around mempalace.KnowledgeGraph.

    Adds:
    - Deterministic triple PK derived from (s,p,o,valid_from,source) so
      replay / NAK retries don't double-insert.
    - Shares ``LockedBackend.lock`` for cross-table read coherence.
    """
    def __init__(self, inner: KnowledgeGraph, lock: asyncio.Lock) -> None:
        self._inner = inner
        self._lock = lock

    @staticmethod
    def _triple_pk(subject, predicate, obj, valid_from, source_turn_id) -> str:
        key = f"{subject}|{predicate}|{obj}|{valid_from}|{source_turn_id}"
        return "triple_" + hashlib.sha256(key.encode()).hexdigest()[:24]

    async def add_triple(self, *, subject, predicate, obj, valid_from, valid_to,
                         confidence, source_turn_id, adapter_name) -> str:
        triple_id = self._triple_pk(subject, predicate, obj, valid_from, source_turn_id)
        async with self._lock:
            # Use direct INSERT OR IGNORE on the underlying SQLite to avoid
            # mempalace's uuid4 path. The schema in mempalace allows this.
            await asyncio.to_thread(
                self._inner._conn().execute,
                """INSERT OR IGNORE INTO triples
                   (id, subject, predicate, object, valid_from, valid_to,
                    confidence, source_drawer_id, adapter_name, extracted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (triple_id, subject, predicate, obj, valid_from, valid_to,
                 confidence, source_turn_id, adapter_name),
            )
            self._inner._conn().commit()
        return triple_id

    async def invalidate(self, *, subject, predicate, obj, ended) -> int:
        async with self._lock:
            # idempotent: take the EARLIEST ended timestamp (don't overwrite)
            return await asyncio.to_thread(
                self._inner._conn().execute,
                """UPDATE triples SET valid_to = MIN(COALESCE(valid_to, ?), ?)
                   WHERE subject=? AND predicate=? AND object=?
                     AND (valid_to IS NULL OR valid_to > ?)""",
                (ended, ended, subject, predicate, obj, ended),
            ).rowcount

    # 读路径（共享同一把锁）
    async def query_entity(self, name, as_of=None, direction="outgoing"): ...
    async def timeline(self, entity_name=None, limit=100): ...
    async def stats(self): ...
```

**对外签名与 mempalace 兼容**——upstream 升级时只需 patch 内部实现，调用方不动。

### 3.1 新增文件

| 文件 | 角色 |
|------|------|
| `eidolon/memory/adapters/locked_kg.py` | `LockedKnowledgeGraph` 见 §3.0；与 `LockedBackend.lock` 共用 |
| `eidolon/memory/domain/kg.py` | `KgTriple` / `KgEntity` / `KgInvalidation` / `KgAddTripleCommand` / `KgInvalidateCommand` pydantic 模型 |
| `eidolon/memory/infrastructure/bus/commands.py` | `MemoryCommandPayload` 联合体 + 类型 discriminator |

### 3.2 修改文件

| 文件 | 改动 |
|------|------|
| `entrypoints/agent_runner.py` | 启动顺序：`ensure_palace_initialized` → `mempalace.KnowledgeGraph(db_path=..).close()` 显式建表 fsync → 对 chroma + KG **两份** SQLite 跑 integrity_check → 构造 LockedKnowledgeGraph → 启 NATS 双订阅（turn + cmd，见 §3.4）|
| `entrypoints/mcp_server.py` | 注册 KG 工具（见 §3.3）；**写工具仅 publish 不直写** |
| `infrastructure/integrity.py` | `run_integrity_check` 支持任意 SQLite 文件；调用方对两份 SQLite 各跑一次 |
| `infrastructure/bus/subjects.py` | 新增 `memory_command_subject(user_id)` + `memory_command_stream_pattern()` |
| `infrastructure/nats_stream.py` | stream 绑定加 `agent.memory.cmd.>` |
| `infrastructure/nats/commands.py` | 新增 `JetStreamCommandPublisher`（与 turn 共享 connection）+ `_request_blocking` |
| `application/turn_processor.py` | 拆出 `process_command_message`，与 turn 同 worker 内并联处理 |
| `scripts/snapshot_palaces.sh` | 同时 checkpoint `chroma.sqlite3` **和** `knowledge_graph.sqlite3` |
| `application/livekit_recall.py` | **不动**（T3 才碰） |

### 3.3 MCP 工具签名（写=publish，读=直查；admin 受同样约束）

#### 写工具（**publish 到 NATS**，等可见再返回，体验是 sync 的）

```python
@mcp.tool()
async def eidolon_memory_kg_add_triple(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence: float = 1.0,
    source_drawer_id: str | None = None,
    wait_visible_seconds: float = 2.0,    # 短轮询窗口
) -> dict[str, Any]:
    """Queue a KG triple write via NATS; polls until visible or timeout.

    All admin/agent writes flow through the same NATS pipeline as chat turns,
    so JetStream remains the single source of truth and rebuild-from-replay
    recovers admin edits identically to chat-extracted triples.
    """
    request_id = uuid.uuid4().hex
    valid_from_iso = valid_from or _now_iso()
    triple_pk = LockedKnowledgeGraph._triple_pk(
        subject, predicate, object, valid_from_iso, f"req:{request_id}"
    )

    payload = KgAddTripleCommand(
        request_id=request_id, user_id=user_id, subject=subject,
        predicate=predicate, object=object, valid_from=valid_from_iso,
        valid_to=valid_to, confidence=confidence, source_drawer_id=source_drawer_id,
        source_turn_id=f"req:{request_id}", adapter_name="admin",
    )
    await command_publisher.publish(payload)

    # Sync-feel polling (cheap: KG indexed lookup by triple_pk)
    deadline = time.monotonic() + wait_visible_seconds
    while time.monotonic() < deadline:
        if await locked_kg.has_triple(triple_pk):
            return {"status": "applied", "triple_id": triple_pk, "request_id": request_id}
        await asyncio.sleep(0.03)   # 30ms 节奏，期望 ~3-4 次轮询
    return {"status": "pending", "triple_id": triple_pk, "request_id": request_id}

@mcp.tool()
async def eidolon_memory_kg_invalidate(
    subject: str, predicate: str, object: str,
    ended: str | None = None,
    wait_visible_seconds: float = 2.0,
) -> dict[str, Any]:
    """Mark a triple as ended via NATS. Returns when worker has applied it."""
    # 同 add_triple 范式：publish + poll until KG's matching triple has valid_to set
```

**为什么 sync-feel UX 而不是真 sync**：
- 真 sync = MCP 直写 KG，违反铁律 1
- 异步 ack 但不等可见 = admin 看到"queued" 困惑
- publish + 短轮询 = NATS 单源真理 + 用户体验"立刻看到"
- 典型可见延迟：50-100ms（worker fetch + steward bypass + KG indexed insert）
- worker 慢时回退 `{status:"pending", request_id}`，admin 可以稍后查或忽略

#### 读工具（直查 LockedKnowledgeGraph，无副作用，no NATS detour）

```python
@mcp.tool()
async def eidolon_memory_kg_query_entity(
    name: str,
    as_of: str | None = None,
    direction: str = "outgoing",
    include_sensitive: bool = False,    # G2: 默认排除 health_*/medication 谓词
) -> dict[str, Any]:
    """User-bound entity query. Sensitive predicates require explicit opt-in."""

@mcp.tool()
async def eidolon_memory_kg_timeline(
    entity_name: str | None = None,
    limit: int = 100,
    include_sensitive: bool = False,
) -> dict[str, Any]: ...

@mcp.tool()
async def eidolon_memory_kg_stats() -> dict[str, Any]: ...

@mcp.tool()
async def eidolon_memory_kg_predicates() -> dict[str, Any]:
    """Return the canonical predicate whitelist + brief Chinese description.

    Lets admin / IDE clients introspect the schema without reading source.
    """
```

### 3.4 NATS subject / payload 设计（G6 admin 走 NATS 的核心）

```
Stream MEMORY_TURNS 绑定两类 subject:
  agent.memory.conversation.turn.<user_id>     (existing, chat turns)
  agent.memory.cmd.<user_id>                   (NEW, manual commands)

Payload (discriminated union):
  ConversationTurnPayload        (existing)
  KgAddTripleCommand             (NEW)
  KgInvalidateCommand            (NEW)
  MemoryDeleteCommand            (NEW, 取代旧 NATS RPC 的删除)

每个 command:
  request_id: str                  # admin sync-feel polling 用
  user_id: str                     # subject 已有，payload 内做防错校验
  issued_at: str                   # ISO-8601 issue time
  issuer: Literal["admin", "agent"] = "admin"
```

worker 内部用**两个并行 pull-subscription**，turn 和 cmd 拉取竞争同一把 LockedBackend.lock。**cmd 优先级与 turn 相同**——单用户写量极低，无需复杂优先级队列。

**命令也享受 JetStream 14 天 retention**：rebuild-from-jetstream 同时重放 turn + cmd → admin 编辑天然包含在恢复路径内。**没有"导出导入"步骤**。

### 3.4 单测要点

- LockedKnowledgeGraph 串行化（read+write 都过同一把 lock）
- KG SQLite 文件 PRAGMA integrity_check 启动期通过
- MCP 工具注册 + 调用 round-trip
- 跨进程隔离：T1 不要尝试，留 T3 处理

### 3.5 验收（T1）

- 启 agent_runner，通过 MCP client 调 `add_triple` × 5；`query_entity` 拿回；`invalidate`；再 `query_entity` 不再含被 invalidate 的
- KG 文件出现在 palace 目录；`stats()` 返回正确计数
- pytest 全绿

---

## 4. Tier 2：Steward 自动写入

### 4.1 设计要点（参考 [OpenAI cookbook on temporal triple extraction](https://developers.openai.com/cookbook/examples/partners/temporal_agents_with_knowledge_graphs/temporal_agents)）

OpenAI cookbook 推荐**三阶段** prompt（Statement extraction → Date extraction → Triplet
extraction），分别用三次 LLM 调用。对陪伴单机场景太重，**简化为单次调用**输出
fragments + triples + invalidations 三个数组。代价是抽取精度略低，可用
`confidence` 字段过滤低信度结果。

### 4.2 StewardDecision schema 扩展（G10 严格谓词白名单）

```python
# eidolon/memory/domain/kg.py
KgPredicate = Literal[
    # 人际关系
    "child_of", "parent_of", "partner_of", "sibling_of", "friend_of", "colleague_of",
    # 身份/角色
    "works_at", "lives_in", "studies_at", "holds_role", "born_in",
    # 偏好
    "likes", "dislikes", "prefers",
    # 行为/活动
    "does", "practices", "owns", "uses",
    # 承诺/事项
    "promised", "committed_to", "planned_to",
    # 状态
    "has_state", "has_emotion", "has_concern", "worried_about", "struggles_with",
    # 健康（敏感，默认 KG read 工具不返回）
    "has_health_condition", "takes_medication", "has_symptom",
    # 事件（一次性）
    "attended", "experienced", "achieved",
]

SENSITIVE_PREDICATES = frozenset({
    "has_health_condition", "takes_medication", "has_symptom",
})

# eidolon/memory/domain/steward.py
class KgTripleAction(BaseEidolonModel):
    subject: str = Field(min_length=1, max_length=128)
    predicate: KgPredicate                            # 严格 Literal；越界整条丢弃
    object: str = Field(min_length=1, max_length=256)
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)

class KgInvalidationAction(BaseEidolonModel):
    subject: str = Field(min_length=1)
    predicate: KgPredicate
    object: str = Field(min_length=1)
    ended: str | None = None
    reason: str = Field(default="", max_length=128)

class StewardDecision(BaseEidolonModel):
    should_write: bool
    reason: str = ""
    fragments: list[MemoryFragment] = []
    triples: list[KgTripleAction] = []
    invalidations: list[KgInvalidationAction] = []
    privacy_actions: list[PrivacyAction] = []
```

**LLM 输出 invalid JSON 或谓词越界 → pydantic ValidationError → 整条 decision 退化为
`should_write=False, ...fragments=[], triples=[]`**（不写任何东西）。下一轮再来。
不允许"部分接受"，否则 KG 会被 LLM 幻觉慢慢污染。

### 4.3 完整 LLM Steward Prompt（中文，陪伴场景特化）

写入 `eidolon/memory/config/prompts/memory_steward.md`，替换/扩展现有模板。

```markdown
你是陪伴智能体「{{ assistant_name }}」的记忆 steward。一段用户和 AI 的对话已结束。
你的任务：基于这段对话，决定要不要写入长期记忆，以及写哪些。

输出严格的 JSON，遵循下方 schema。**不要**输出 markdown 代码块包装，不要解释。

# Wings 信息架构（chromadb drawers 的归属）

{{ wings_block }}

# 输出 schema

{
  "should_write": <bool>,
  "reason": <string，30 字内总结判断依据>,
  "fragments": [                  // 写入向量库（chromadb drawers）
    {
      "fragment_id": "",
      "user_id": "{{ user_id }}",
      "wing": "<wing id，必须取自上方 wings 列表>",
      "room": "<room slug，规则见下>",
      "content": "<自然中文短句，单一记忆>",
      "memory_type": "profile | interaction | relationship | emotion | goal | event | work | life | health | preference | privacy | commitment",
      "importance": <1-5>,
      "confidence": <0.0-1.0>,
      "occurred_at": "<ISO-8601 或空>",
      "source_turn_id": "{{ turn_id }}",
      "session_id": "{{ session_id }}",
      "tags": [<string>...],
      "privacy": "normal | private | do_not_recall",
      "metadata": {}
    }
  ],
  "triples": [                    // 写入知识图谱（temporal facts）
    {
      "subject": "<entity 名，必须用 canonical 形式，见下>",
      "predicate": "<受限谓词集合中的一个，见下>",
      "object": "<entity 名 或 字面值>",
      "valid_from": "<ISO-8601；省略则默认当前 turn 时刻>",
      "valid_to": "<ISO-8601 或 null；通常 null>",
      "confidence": <0.0-1.0>
    }
  ],
  "invalidations": [              // 标记旧三元组失效（处理改变心意/兑现承诺）
    {
      "subject": "...",
      "predicate": "...",
      "object": "...",
      "ended": "<ISO-8601；省略则默认当前 turn 时刻>",
      "reason": "<30 字内说明，给运维>"
    }
  ],
  "privacy_actions": [
    {
      "action": "do_not_store | delete_request | archive_topic",
      "target": "<话题或 drawer 标识>",
      "reason": "<用户原话或推断>"
    }
  ]
}

# Fragments vs Triples 的区分

- **fragments** = 自然语言级别的记忆"段落"，未来通过语义相似度召回（"那时候很难过的事"）
- **triples** = 结构化"关于某实体的当前事实"，通过实体名 + 时间窗口查询（"妈妈最近怎么样"）

**经验法则**：
- 涉及具体人物/项目的**关系或状态** → triple（也可同时写 fragment 作为原文留存）
- 用户的情绪/感受/想法 → 通常只写 fragment
- 改变心意 / 承诺兑现 → 一个 invalidation + 可选一个新 triple
- 寒暄 / 一次性闲聊 → 都不写

# Triples 的受限谓词集合（time-neutral）

人际关系：
  child_of, parent_of, partner_of, sibling_of, friend_of, colleague_of

身份/角色：
  works_at, lives_in, studies_at, holds_role, born_in

偏好：
  likes, dislikes, prefers

行为/活动：
  does, practices, owns, uses

承诺/事项：
  promised (object 应为可执行短语，valid_to 是兑现期限),
  committed_to, planned_to

状态（时态性强，valid_to 通常需要在状态结束时通过 invalidation 补）：
  has_state, has_emotion, has_concern, worried_about, struggles_with

健康：
  has_health_condition, takes_medication, has_symptom

事件（一次性时刻，valid_from = valid_to）：
  attended, experienced, achieved

不要发明新谓词。不在列表里的关系，要么折成已有谓词，要么写成 fragment。

# 实体规范化（canonical 名约定）

- 第一人称"我/我自己" → `self`
- "妈妈/我妈/老妈" → `mother`（如对话里提到具体名字如"张丽"，用 `mother:张丽`）
- "爸爸/我爸/老爸" → `father`，同理
- 其他亲属："姐姐/姐"→`sister:<名>`、"哥/哥哥"→`brother:<名>`、"老婆/媳妇/妻子"→`wife:<名>`、"老公/丈夫"→`husband:<名>`
- 提到的朋友/同事：第一次出现用 `person:<原称呼>`；同对话内重复用同一名字
- 工作项目：`project:<项目代号或简称>`
- 地点：`place:<地名>`
- 抽象概念（不是实体）：直接用字符串字面值（如 `coffee`、`insomnia`、`anxiety`）

绝对禁止：
- 用代词作 subject 或 object（"她/他/它"）
- 用未来时刻的事件作为 add_triple（写 fragment 表达计划即可，除非用户明确"已经决定"）

# 改变心意 / 承诺兑现的处理流程

用户表达**否定**：「我现在不喜欢咖啡了」
→ invalidations 加 `{subject:"self", predicate:"likes", object:"coffee", ended:"<turn 时刻>"}`
→ 通常**不需要** add 新 triple

用户表达**新偏好**：「我现在喜欢茶」
→ triples 加 `{subject:"self", predicate:"likes", object:"tea", valid_from:"<turn 时刻>"}`

用户**承诺**：「这周末陪妈妈去医院」
→ triples 加 `{subject:"self", predicate:"promised", object:"陪 mother 去医院", valid_from:"<turn 时刻>", valid_to:"<本周日 23:59>"}`

用户**兑现承诺**：「我已经陪妈妈去过医院了」
→ invalidations 加 `{subject:"self", predicate:"promised", object:"陪 mother 去医院", ended:"<turn 时刻>", reason:"已兑现"}`
→ 同时 triples 加 `{subject:"self", predicate:"attended", object:"陪 mother 去医院", valid_from:"<事件时刻>", valid_to:"<事件时刻>"}`

# 隐私优先

用户说"不要记住 / 别记录 / 忘掉" → fragments / triples / invalidations 全部留空；
只在 privacy_actions 输出 do_not_store / delete_request / archive_topic。

# 不确定就别写

宁缺勿滥。模型推断、用户未确认的事实**不要**输出。
confidence < 0.6 的 triple 一律放弃。

# 输入

turn_id: {{ turn.turn_id }}
user_id: {{ turn.user_id }}
session_id: {{ turn.session_id }}
timestamp: {{ turn.timestamp }}

[USER]
{{ turn.user_text }}

[ASSISTANT]
{{ turn.assistant_text }}
```

### 4.4 worker 写入流程修改（G7 KG 不阻断 ack + G8 可观测）

`application/turn_processor.py`：

```python
async def process_turn_message(msg, *, steward, backend, kg, settings, max_deliveries, expected_user_id):
    # ... 解析、user_id 校验同前 ...
    try:
        decision = await steward.decide(turn)
    except Exception as exc:
        # steward 故障 → 现有 NAK/DLQ 逻辑不变
        ...
        return

    # ─── chroma drawers 写入：失败 → NAK，因为 fragment 是 source of truth ───
    fragments_written = 0
    try:
        async with backend.lock:
            await apply_privacy_actions(backend, ...)
            if decision.should_write:
                for fragment in decision.fragments:
                    await backend.ingest_fragment(fragment)
                    fragments_written += 1
    except Exception as exc:
        log.error("turn_processor_fragment_failed", error=str(exc), deliveries=deliveries)
        await _nak_or_dlq(msg, deliveries, settings, max_deliveries)
        return

    # ─── KG 写入：失败 → log 但 ack（G7 KG 不阻断 chat） ───
    kg_triples_added = 0
    kg_invalidations_applied = 0
    kg_skipped_low_confidence = 0
    kg_failures: list[str] = []
    try:
        async with backend.lock:
            for inv in decision.invalidations:
                try:
                    rows = await kg.invalidate(
                        subject=inv.subject, predicate=inv.predicate, obj=inv.object,
                        ended=inv.ended or turn.timestamp,
                    )
                    if rows > 0:
                        kg_invalidations_applied += 1
                    else:
                        log.info("kg_invalidate_no_match",
                                 subject=inv.subject, predicate=inv.predicate, object=inv.object)
                except Exception as exc:
                    kg_failures.append(f"inv:{exc}")
            for t in decision.triples:
                if t.confidence < settings.kg.min_confidence_to_write:
                    kg_skipped_low_confidence += 1
                    continue
                try:
                    await kg.add_triple(
                        subject=t.subject, predicate=t.predicate, obj=t.object,
                        valid_from=t.valid_from or turn.timestamp,
                        valid_to=t.valid_to,
                        confidence=t.confidence,
                        source_turn_id=turn.turn_id,
                        adapter_name="steward-llm",
                    )
                    kg_triples_added += 1
                except Exception as exc:
                    kg_failures.append(f"add:{exc}")
    except Exception as exc:
        # 锁本身的失败不应该静默
        log.error("turn_processor_kg_outer_failed", error=str(exc))

    # G8 observability: 一行日志总结 turn 的全部影响
    log.info(
        "turn_processed",
        turn_id=turn.turn_id, user_id=turn.user_id,
        fragments=fragments_written,
        triples=kg_triples_added,
        invalidations=kg_invalidations_applied,
        kg_skipped_lowconf=kg_skipped_low_confidence,
        kg_failures=len(kg_failures),
        kg_failure_sample=kg_failures[:2],
    )
    await msg.ack()
```

`process_command_message`（新增，给 admin 走 NATS 用）：

```python
async def process_command_message(msg, *, backend, kg, settings):
    cmd = MemoryCommandPayload.model_validate_json(msg.data)
    if cmd.kind == "kg_add_triple":
        try:
            async with backend.lock:
                await kg.add_triple(
                    subject=cmd.subject, predicate=cmd.predicate, obj=cmd.object,
                    valid_from=cmd.valid_from, valid_to=cmd.valid_to,
                    confidence=cmd.confidence,
                    source_turn_id=f"req:{cmd.request_id}",
                    adapter_name=cmd.adapter_name,
                )
            log.info("cmd_kg_add_ok", request_id=cmd.request_id, predicate=cmd.predicate)
        except Exception as exc:
            log.error("cmd_kg_add_failed", request_id=cmd.request_id, error=str(exc))
        await msg.ack()   # admin 命令始终 ack（重投只会创建重复 request_id，由 §3.0 幂等吞掉）

    elif cmd.kind == "kg_invalidate":
        # 同上
        ...
```

### 4.5 配置新增

```yaml
kg:
  min_confidence_to_write: 0.6     # T2: 低信度 triple 不写（pydantic 已校 0.0-1.0）
  predicate_whitelist_enforced: true   # G10：true = pydantic Literal 严格生效
```

### 4.6 验收（T2，更新）

- 5 条对话（偏好/关系/承诺/改变心意/隐私拒记）→ KG triples 命中预期
- "改变心意"那条 → 旧 triple `valid_to` 被填充，新 triple 出现
- "用户说别记" → fragments **和** triples **都**为空
- LLM 输出 invalid JSON / 越界谓词 → 整条 decision 退化，**不污染 KG**
- worker 在 KG 写失败时仍 ack chat turn（chat 完成 + 一行 `turn_processed` 日志含 kg_failures>0）
- 同一 turn 二次投递 → KG 中**不增加新 triple**（G1 幂等验证）
- pytest 全绿

### 4.7 Steward 提示词 Eval Set（G9，**T2 上线前必跑**）

LLM 抽取的精度/召回**必须量化**，否则 KG 被幻觉污染。建议规模：**20-25 条手工标注对话样本**，覆盖：

| 类别 | 样本数 | 期望输出 |
|------|------:|---------|
| 关系陈述（"我妈是医生"） | 5 | 1-2 triples（人际/职业），fragments=relationship 类型 |
| 偏好转变（"现在不喝咖啡了"） | 3 | 1 invalidation + 可选 0-1 triple，fragments 反映心情 |
| 承诺/兑现（"答应妈妈周末"） | 3 | 1 promised triple，valid_to=本周末；兑现样本含对应 invalidation |
| 健康事项（"我有高血压"） | 3 | has_health_condition triple；fragments privacy=normal（用户主动告知） |
| 隐私拒记（"刚才那个别记"） | 3 | fragments=[] triples=[]，privacy_actions 含 archive_topic |
| 寒暄无入图（"今天天气不错"） | 3 | should_write=false |
| 多事件混合（"今早跟妈妈吵架但晚上和好了"） | 2-3 | 同 turn 内含 attended + has_state + invalidation 复合输出 |

路径：

1. 写到 `tests/memory/eval_steward_dataset.jsonl`（gitignored，本地）
2. 写一个 `scripts/benchmark/eval_steward_prompt.py`：迭代 dataset，调 LLM，与期望 diff
3. **指标门槛**：
   - Triples precision ≥ **0.85**（不应该有的 triple 写出来了的比例）
   - Triples recall ≥ **0.70**（应该有的 triple 没写出来的比例）
   - Invalidations precision ≥ **0.90**（误 invalidate 后果更严重，门槛更高）
   - 隐私漏写率 = **0**（"别记"被记了一次都算 fail）
4. 不达标 → 调 prompt（增加 few-shot example，收紧规则）继续迭代

工作量：dataset 标注 2-3h + eval 脚本 0.5 天。**T2 任何上线决定都基于这份 eval 通过**。

### 4.6 验收（T2）

- 发布 5 条对话（含偏好/关系/承诺/改变心意/隐私拒记）→ KG 中应出现对应 triples
- "改变心意"那条 → 旧 triple `valid_to` 被填充，新 triple 出现
- LLM 输出非 JSON 时 → fallback 到只写 fragments（不能让 KG 错误阻断 turn 写入）
- pytest 全绿

---

## 5. Tier 3：智能召回（recall_context 内置 KG 融合）

### 5.1 性能预算（300ms LiveKit SLA 内的拆分）

| 阶段 | 在 LockedBackend.lock 内？ | 暖 | 冷 | 备注 |
|------|--------------------------|---:|---:|------|
| Query embedding (ONNX) | 否 | 0ms (LRU hit) | 25-30ms | 唯一冷点 |
| 实体 candidate 抽取 | 否 | <2ms | <3ms | 60s 缓存 |
| Vector 4-wing query (共享 embedding) | **是** | 5-10ms | 5-10ms | 现状 |
| KG combined query (3 entities, 1 SQL IN) | **是** | 3-8ms | 3-8ms | 新增 |
| KG timeline 30d | **是** | 3-5ms | 3-5ms | 新增 |
| Triples → 中文转写 + merge + format | 否 | <3ms | <3ms | |
| HTTP framing (loopback) | 否 | ~10ms | ~10ms | |

**端到端**：
- 暖路径（LRU 命中、KG 命中）≈ **30ms**（与现状 33ms 几乎平）
- 冷路径（embed miss）≈ **55-65ms**
- 极端（KG 50ms 超时降级）≈ **75ms** ← 仍远低于 300ms 上限

**距 300ms SLA 仍有 ≥ 225ms 余量**。

### 5.2 锁模型说明（重要决策）

vector path 与 KG path 都过 `LockedBackend.lock`——即便 `asyncio.gather` 也会**串行化**（锁内一次只一个 chroma/KG 调用）。

- 锁内 vector + 锁内 KG 串行 ≈ 13-18ms
- 真并行（拆两把锁） ≈ max(10, 13) = 13ms
- 差距 5-10ms = **<3% 预算**

→ **保守用单大锁**（与 D1 哲学一致），到 R-01 P95 > 60ms 才考虑拆锁。

### 5.3 路由策略代码骨架

`application/public_recall.py` 内新增 `_kg_path` 段：

```python
async def search_all_wings_mcp_style(
    backend, settings, *, query, user_id, top_k, wing, room,
    for_voice=False, session_id="", user_utterance="", palace_path, kg=None,
):
    # 1. embed (no lock)
    embedding = await asyncio.to_thread(embed_query_vector, query)

    # 2. entity candidates (no lock, cached)
    candidates: list[str] = []
    if kg is not None and settings.recall.kg_in_recall:
        candidates = _extract_entity_candidates(query, kg)[: settings.recall.kg_max_entities]

    # 3. parallel vector + KG (锁串行化 OK，asyncio.gather 写法)
    vector_task = asyncio.create_task(_vector_path(backend, embedding, ...))
    kg_task: asyncio.Task | None = None
    if candidates:
        kg_task = asyncio.create_task(_kg_path_with_timeout(
            backend, kg, candidates,
            window_days=settings.recall.kg_window_days,
            timeout_s=settings.recall.kg_timeout_seconds,    # 0.05
        ))

    vector_records = await vector_task
    kg_records = await kg_task if kg_task else []
    return _merge(vector_records, kg_records, top_k=top_k)


async def _kg_path_with_timeout(backend, kg, entities, *, window_days, timeout_s):
    """One combined SQL for all entities; degrade silently on timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_kg_query_combined, backend, kg, entities, window_days),
            timeout=timeout_s,
        )
    except TimeoutError:
        return []


def _kg_query_combined(backend, kg, entities, window_days):
    """Single SQL with subject IN (?,?,?)—not N separate queries."""
    async with backend.lock:  # 与 vector path 共用同一把锁
        ...
        # SELECT * FROM triples
        # WHERE subject IN (?,?,?)
        #   AND (valid_to IS NULL OR valid_to > :now)
        #   AND valid_from <= :now
        # 加 timeline 一次性查询；3 entities → 1 round-trip
```

### 5.4 实体 candidate 抽取（不调 LLM；G11 用 entities 表）

```python
def _cached_entity_names(kg, ttl_seconds=60) -> list[str]:
    """Cache 60s; uses entities table not DISTINCT subject from triples.

    G11 修补：triples 表长大后 SELECT DISTINCT 会随数据线性慢；
    entities 表是 mempalace KG 的一等公民（每个实体一行），自然 ~N_entities
    而非 ~N_triples。150K triples 下 SELECT DISTINCT subject 可能 50ms+，
    而 SELECT name FROM entities ~ <5ms。
    """
    now = time.monotonic()
    if _entity_cache and now - _entity_cache_ts < ttl_seconds:
        return _entity_cache
    rows = kg._inner._conn().execute("SELECT name FROM entities").fetchall()
    _entity_cache = [r["name"] for r in rows]
    _entity_cache_ts = now
    return _entity_cache

def _extract_entity_candidates(query: str, kg) -> list[str]:
    """O(N) 字符串子串命中，<2ms warm。"""
    all_entities = _cached_entity_names(kg)
    return [e for e in all_entities if e in query]   # cap 在 caller
```

阈值与 cap：
- 实体名单缓存 TTL 60s（worker 写入时**主动 invalidate** 防新 entity 等 60s）
- candidates cap = **3**（plan v1 写过 5，**改 3**，控制单条 SQL `IN (?,?,?)` 大小）

### 5.4.1 缓存 invalidation 时机

worker 在 `process_command_message` / `process_turn_message` 里**新增 entity 时**调一次：
```python
_entity_cache_ts = 0   # 强制下次读时刷新
```
不需要 LRU lib，简单 module-global TTL 即可。

### 5.5 KG → context 转写

```python
def _kg_triple_to_context(t: dict) -> str:
    # 把 (self, promised, 陪 mother 去医院, valid_from=..., valid_to=...)
    # 转成自然中文 + 时态信号
    # "[承诺] 自 2026-05-15 起：陪 mother 去医院（截至 2026-05-19）"
```

### 5.6 配置新增

```yaml
recall:
  kg_in_recall: true                # 总开关
  kg_timeout_seconds: 0.05          # 硬超时，超了走纯 vector
  kg_window_days: 30                # timeline 默认窗口
  kg_max_entities: 3                # 单 query 最多匹配 3 个 entity
  kg_max_triples_per_entity: 8      # 每实体最多注入 context 几条
  kg_entity_cache_ttl_seconds: 60   # entity 名单缓存
```

### 5.7 验收（T3）

- **KG-V7 性能回归**：跑 R-01 voice（seed S=100 + KG 50 triples），P95 增量 ≤ 30ms（**绝对值 ≤ 65ms**）
- **KG-V8 KG 慢降级**：mock KG 100ms 延迟 → KG path 触发 50ms 超时，recall 仍 < 300ms 完成
- **KG-V10 实体命中正确**：提问"我妈最近怎么样" → context 含 KG timeline；提问"工作压力" → context 走纯 vector path（KG 0 entity 命中）
- **KG-V11 锁竞争监控**：写并发下读 P95 退化 < 15%（与现状 V4 相同标准）

---

## 6. 与已有计划的耦合

| 关联文件 | 影响 |
|---------|------|
| `docs/architecture-d1-readwrite-split.md` | §3 / §4 不变（KG 与 chroma 共用同一把 LockedBackend.lock） |
| `scripts/rebuild_palace_from_jetstream.py` | replay 时只跑 steward，KG 自动重建；不需改 |
| `scripts/snapshot_palaces.sh` | 已经 tar 整个 palace 目录，KG 自动包含 |
| `eidolon/memory/infrastructure/integrity.py` | 启动期对 KG SQLite 也跑 `PRAGMA integrity_check` |

---

## 7. 验收回归（汇总）

| # | 验收 | 标准 |
|---|------|------|
| KG-V1 | LockedKnowledgeGraph 串行化所有调用（含 query） | grep 验证 + 单测 |
| KG-V2 | KG 文件 PRAGMA integrity_check 启动期通过 | smoke |
| KG-V3 | 6 个 MCP 工具注册 + round-trip | pytest + e2e curl |
| KG-V4 | Steward 输出 invalid JSON 时降级写 fragment 不写 KG | mock LLM |
| KG-V5 | "改变心意" turn → 旧 triple invalidated，新 triple 出现 | e2e |
| KG-V6 | "承诺" turn → triple 有 valid_to | e2e |
| KG-V7 | recall_context 启用 KG 后 P95 增量 ≤ 30ms（seed S=100） | bench |
| KG-V8 | KG 超时 0.05s 触发降级时 recall 不挂 | mock 慢 KG |
| KG-V9 | snapshot tar 后 KG.sqlite3 完整 | 解压 + integrity_check |
| KG-V10 | 实体路由正确 ("我妈最近"命中 KG / "工作压力"走纯 vector) | 手动 + 单测 |
| KG-V11 | 写并发下读 P95 退化 < 15%（与现状 V4 相同标准） | 并发 bench |
| **KG-V12** | **G1 幂等**：同一 (s,p,o,turn_id) 二次 add → KG triple 数不变 | 单测 |
| **KG-V13** | **G2 隐私**：用户拒记 turn → fragments=[] triples=[]；KG-read 工具默认不返回 `has_health_condition` 等敏感谓词 | mock turn + read 工具调用 |
| **KG-V14** | **G3 启动顺序**：fresh palace 启 agent_runner → KG.sqlite3 创建 + integrity_check ok | smoke |
| **KG-V15** | **G6 admin via NATS**：MCP `kg_add_triple` 工具 publish 后 ≤ 200ms 可见 + JetStream stream 内能看到 cmd payload | e2e |
| **KG-V16** | **G7 KG 失败不阻 chat**：mock KG raise → fragment 仍 ingest 成功 + msg ack + 日志含 `kg_failures` | mock |
| **KG-V17** | **G8 可观测**：`turn_processed` 日志结构含 7 个字段（fragments/triples/invalidations/skipped/failures/...） | grep |
| **KG-V18** | **G9 eval set**：steward prompt 在 20+ 标注样本上 precision≥0.85 / recall≥0.70 / 隐私漏写=0 | eval_steward_prompt.py |
| **KG-V19** | **G10 谓词严格**：LLM 输出非白名单谓词 → 整条 decision 退化（fragments/triples 都不写） | mock LLM |
| **KG-V20** | **G11 实体 cache 性能**：150K triples 下 `_cached_entity_names` P95 < 10ms | bench |

---

## 8. 落地分阶段（v1.1，含铁律 + 11 项 gap 修补）

| 阶段 | 任务 | 工作量 |
|------|------|--------|
| **T1-A** | `domain/kg.py` schema (含 `KgPredicate` Literal) + `adapters/locked_kg.py` 包装（G1 幂等 + 共享锁）+ 单测 | 1 天 |
| **T1-B** | agent_runner 启动顺序：palace init → KG.close() 建表 → 双 integrity check（G3）；snapshot 脚本两份 SQLite 一起 checkpoint（G4） | 0.5 天 |
| **T1-C** | NATS subject + command publisher + worker `process_command_message`（admin via NATS，G6 铁律）| 1 天 |
| **T1-D** | 6 个 MCP 工具：4 个写工具 publish + 短轮询，2 个读工具直查 + 敏感谓词默认排除（G2） | 1 天 |
| **T1-E** | e2e smoke：admin 工具 add → 200ms 内可见；JetStream replay 重建 KG 含 admin 写 | 0.5 天 |
| **T2-A** | StewardDecision schema 扩展（KgPredicate Literal，G10） + 写新 prompt（隐私覆盖 triples，G2） | 1 天 |
| **T2-B** | **构建 Eval set 20+ 标注样本 + `eval_steward_prompt.py`** + 迭代 prompt 到达标 (G9) | **1.5 天** ← 不可省略 |
| **T2-C** | turn_processor 接 KG 写 + G7 fail-safe + G8 `turn_processed` 结构化日志 | 1 天 |
| **T2-D** | 回归测：mock LLM bad JSON、谓词越界、重投幂等、KG raise 不阻 ack | 0.5 天 |
| **T3-A** | candidate 抽取（用 entities 表，G11）+ KG combined SQL `IN (?,?,?)` | 1 天 |
| **T3-B** | recall_context 真并行 asyncio.gather + 50ms 硬超时 + triple → 中文转写 | 1.5 天 |
| **T3-C** | bench 增 `--with-kg` flag，跑 KG-V7（P95 增量 ≤ 30ms）、KG-V11（写并发不退化）| 1 天 |

**总计 ~11.5 天**（v1.0 是 8-9 天；v1.1 增加来自：admin via NATS 1 天 + eval set 1.5 天 + 各 gap 修补 1 天）。建议每 Tier 完成 commit；T1-C / T2-B 是关键 milestone，独立 commit。

---

## 9. 不在本计划范围（再下一计划）

- **召回质量**: hybrid（BM25+dense+RRF）、cross-encoder reranker
- **实体自动 NER**: 用 `mempalace.entity_detector` 全自动抽实体（先靠 LLM steward 抽，准确率不够再上 NER）
- **KG 可视化**: admin web 端画图（用 vis-network 或 Cytoscape）
- **跨用户 KG 共享 / 联邦**: 个人陪伴永远不该跨用户共享

---

## 10. 主要参考

- [mempalace.knowledge_graph 源码](.venv/lib/python3.12/site-packages/mempalace/knowledge_graph.py) — 接入对象
- [OpenAI Cookbook: Temporal Agents with Knowledge Graphs](https://developers.openai.com/cookbook/examples/partners/temporal_agents_with_knowledge_graphs/temporal_agents) — Prompt 设计参考
- [Zep / Graphiti arxiv 2501.13956](https://arxiv.org/abs/2501.13956) — temporal KG 架构论证
- [Calmops Hybrid RAG Guide 2026](https://calmops.com/ai/hybrid-search-rag-complete-guide-2026/) — vector+KG 融合策略
- [EDC: Extract-Define-Canonicalize](https://arxiv.org/abs/2404.03868) — 实体规范化框架（T3 演进方向）
