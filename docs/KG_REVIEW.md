# 知识图谱：现状梳理，供 review

写于 2026-08-06，动手改之前。三条并行的代码通读（存储层、端到端管线、与 mempalace 的对比）加上我自己复核的几条。

**标注约定**：带 ✅ 的是我自己跑过或逐行读过确认的；其余是通读结论，都附了 `file:line`，但没有单独复现。

---

## 0. 一句话

图这一层**设计得比向量那一层更完整**——audience 是列不是过滤、时间戳写入时归一、按 subject 分配预算用窗口函数——但它**接线松**：有一个工具根本调不通，一个配置项穿了三层然后被丢掉，一个专门为消除竞态写的方法没有任何生产调用者，而 `mempalace repair` 会静默丢掉六个 ledger。

---

## 1. 全景

```
domain/kg_port.py        KnowledgeGraphPort：4 写 + 8 读 + 3 幂等探针 + close
domain/kg.py             KgTripleRecord / KgTripleAction / KgInvalidationAction
domain/predicates.py     32 个谓词的产品语义（时间性、敏感性）
contracts/.../kg.py      KgPredicate 白名单（Literal）+ SENSITIVE_PREDICATES
contracts/.../audience.py readable_audiences / OWNER / companion:<id>
   │
adapters/kg_sql.py       schema + SQL 片段（VALID_AT / SELECT_COLUMNS / SUBJECT_RANK）
adapters/kg_sqlite.py    唯一实现
   │
application/kg_recall.py         query_kg_for_recall + transcribe_triple + _PREDICATE_ZH
application/public_recall.py     recall_with_kg_fusion（向量 ‖ 图）
application/turn_processor.py    写路径 + 命令路径
application/canonical_invalidation.py   canonical ledger ↔ 图
entrypoints/mcp_server.py        7 个 kg_* 工具（全在 /ops/mcp）
```

**测试**：`test_kg_sqlite.py` 41、`test_kg_recall_fusion.py` 25、`test_kg_command_flow.py` 12、`test_canonical_projection_paths.py` 12、`test_kg_optional.py` 6、`test_kg_fusion_integration.py` 5。

---

## 2. 数据模型

三张表，每张都带 `space_id`（本地每 space 一个文件，这列是冗余的——`kg_sql.py:22-28` 说明是故意的纵深防御，且让"一个共享库"和"一 space 一文件"跑同样的语句）。

| 表 | 关键列 |
|---|---|
| `kg_entities` | `(space_id, entity_id)` PK、`name`、`entity_type`、`properties`、`created_at` |
| `kg_statements` | `(space_id, statement_id)` PK、`subject_id`/`predicate`/`object_id`、**`audience`**、**`sensitive`**、`valid_from`/`valid_to`/`recorded_at`、`confidence`、`source_turn_id`、`adapter_name` |
| `kg_entity_mentions` | `(space_id, mention_id)` PK、`entity_id`、`alias`、`source`、`confidence` |

6 个索引，全部 `space_id` 打头，其中 `(space_id, subject_id, predicate)` 是召回路径的、`(space_id, source_turn_id)` 是幂等探针的。

**空转的列**：`entity_type` / `properties` 永远写 `'unknown'` / `'{}'`（`kg_sqlite.py:270`），从不被读；`kg_entity_mentions.confidence` 写了从不读。`PRAGMA foreign_keys=ON` 开着，但三张表一个 `REFERENCES` 都没有。

---

## 3. 时间模型

**半开区间**，`VALID_AT`（`kg_sql.py:106-118`）：

```sql
(s.valid_from IS NULL OR s.valid_from <= ?) AND (s.valid_to IS NULL OR s.valid_to > ?)
```

所以 supersede 在同一瞬间换值时，不存在"两条都成立"或"两条都不成立"的时刻。

**写入时归一**（`canonical_temporal`，`kg_sqlite.py:62-88`）：date-only 补成 `T00:00:00Z`，其余转 UTC 秒级 `%Y-%m-%dT%H:%M:%SZ`；**解析不了就原样存**——上游是 LLM 输出，"因为日期格式丢掉一条陈述"比"存一个排不了序的时间戳"更糟。

这一条正是相对 mempalace 的核心改进：他们把 date-only 窄存着，在**每个比较里**用 `CASE WHEN length(col)=10 ...` 现场加宽（`mempalace/knowledge_graph.py:87-92`），而那个 `CASE` 包住了索引列，`idx_triples_valid` 因此用不上。

**⚠️ 双时间只做了一半。** `kg_port.py:18-22` 承诺"有效时间与记录时间分离"。`recorded_at` 存了（NOT NULL），但**从不出现在任何 WHERE 里**——只在 ORDER BY 和 id 哈希里。没有"我们在 X 日相信什么"的查询，`KgTripleRecord` 里也没有这个字段，调用方无法自己重建。

---

## 4. 身份

- `entity_id_for(name)` = `strip().lower().replace(" ","_").replace("'","")`。故意合并大小写/空格/撇号；**故意不合并**类型前缀，`pet:铁锤` 和 `铁锤` 是两个实体，靠 `name_appears_in` 在**查询时**桥接。
- `statement_id_for` = `sha256(subject_id ∥ predicate ∥ object_id ∥ valid_from ∥ recorded_at)[:24]`。`audience` **不在哈希里**。
- 显示名**先到先得**（`INSERT OR IGNORE`），第一次写入的拼写永久生效。

**⚠️ 秒级精度的后果**：`recorded_at` 是秒级，所以"加 → 失效 → 同一秒内再加"会哈希碰撞，第二次 `INSERT OR IGNORE` 静默丢弃，且返回那条**已失效**的 id。

---

## 5. 两层可见性

这是相对 mempalace 最实质的差别——**他们的 schema 里完全没有这两列**，`query_entity` 对任何调用方返回文件里的一切。

| | 我们 |
|---|---|
| audience | `audience TEXT NOT NULL` 列，`audience_filter()` 生成 `IN (?,?,…)`，**在 SQL 里过滤** |
| 空集合 | 显式 `if not audiences: return []` 守卫，不是靠 IN 子句 |
| 通配符 | **没有**，运维工具用 `known_audiences()` 枚举真实存在的 |
| sensitivity | `sensitive INTEGER` 列，写入时从 `SENSITIVE_PREDICATES` 推导，**在 SQL 里过滤**——健康事实从不离开存储 |

**没有 audience 过滤的读**：`match_entities_for_query`、`list_entity_names`、`stats`、三个探针。实体**名字**没有 audience 列，所以只因某个 companion 私有陈述而存在的实体，名字是可枚举的（陈述本身仍然被挡住）。

**生产里所有写入都是 `audience=OWNER_AUDIENCE`**——没有任何地方写 companion 层。`README.md:56-59` 把这记为已知状态：按语句判断需要 steward 参与，而默认收窄会把 owner 自己的事实藏起来不给其他 companion 看，是两种错误里更糟的一种。

**两套敏感性登记表**：`SENSITIVE_PREDICATES`（存储层用）和 `PredicateDefinition.sensitive`（MCP 写入闸用），靠 `test_predicates.py:19-21` 断言相等来保持同步，结构上没有派生关系。

---

## 6. 写路径

```
LLM steward → StewardDecision{triples, invalidations, mentions}
   → memory_intents_from_decision（确定性 intent_id，SHA-256）
   → ExtractionDecisionStore（按 (space, turn_id, extractor_version) 幂等）
   → turn_processor：先失效，后新增，最后 mention
```

- **先失效后新增**（`turn_processor.py:402`），这样"改主意"的一轮总是先结束旧事实。
- **`min_confidence_to_write`（默认 0.6）只作用于 triples**；invalidations、mentions、MCP 命令路径、explicit-intent 路径全部绕过。
- **mention 防幻觉**：只接受 `entity_id` 出现在同一轮 triples 的 subject/object 里的（`turn_processor.py:639-656`）。
- **失败模型**：图的写入失败 → 记数 + 日志 + **ack**（聊天不能因为图卡住）。唯一例外是 canonical invalidation 失败 → NAK/DLQ。

**⚠️ 三个问题**：

1. **`should_write=False` 的轮次照样写图** ✅ — fragment 有 `if decision.should_write:`（`:359`）守着，图那一块只有 `if kg is not None:`（`:401`）。
2. **triples 没有条数上限**。`max_fragments_per_turn`（默认 6）只管 fragment，`StewardDecision.triples` 没有 `max_length`，写入循环无界。
3. **rules 兜底 = 零图写入**。LLM 失败落回规则 steward 时，triples/invalidations/mentions 全空。`produced_by` 字段就是为了让这件事可见（上一轮刚加的），但目前**没有任何指标或图路径读它**。

---

## 7. 读路径

```
recall_with_kg_fusion
 ├── 向量任务（asyncio.create_task）
 └── 图任务：match_entities_for_query → query_kg_for_recall
        voice 预算 50ms / chat 300ms，超时 → 静默降级为空
```

**上限**：`kg_max_entities`(3) × `kg_max_triples_per_entity`(8) = 最多 24 条三元组。

图结果**不参与**任何后处理——不重排、不去重、不再截断、不做时间过滤，直接进 `[MEMORY]` 块。渲染在 `transcribe_triple`，`_PREDICATE_ZH` 是 32 条完整模板（上一轮修过的"铁锤 是…的孩子 用户"就在这里）。

**⚠️ 两个问题**：

1. **`recall.kg_window_days`（默认 30）是死配置** ✅ —— `query_kg_for_recall` 把它声明成必填参数（`kg_recall.py:33`），函数体里**一次都没引用**；为它写的 `computed_kg_window_iso`（`:174`）**零调用者**。召回不施加任何时间窗。
2. **`query_entity_combined` 正是 `kg_sql.py` 自己反对的模式**。`kg_sql.py:165-167` 论证"一条语句而不是每个 subject 一次查询，因为对数据库来说每多一次查询就是一次往返"——但 `query_entity_combined` 就是每个实体一次查询、N 次 reader 锁、N 次往返，而且**它是默认路径**：只有调用方显式传了 focus subjects 才走单语句的 `query_subjects`。这条路径跑在 50ms 的 voice 预算里。

---

## 8. MCP 工具面（7 个，全在 `/ops/mcp`）

上一轮的拆分之后，**agent 面一个 kg 工具都看不到**。

| 工具 | 路径 | audience | sensitive |
|---|---|---|---|
| `kg_add_triple` | NATS 写 | 强制 owner | 由谓词推导 |
| `kg_invalidate` | NATS 写 | n/a | n/a |
| `kg_query_entity` | 直读 | `_all_audiences()` 全枚举 | `include_sensitive` 参数 |
| `kg_timeline` | 直读 | `_all_audiences()` | `include_sensitive` |
| `kg_stats` | 直读 | n/a | n/a |
| **`kg_snapshot`** | 直读 | **❌ 没传 audiences** | `include_sensitive` |
| `kg_predicates` | 静态 | n/a | n/a |

**⚠️ 但 agent 面的 `recall_context` 暴露了 `include_sensitive_kg` 参数** —— 对话模型可以自己把 `include_sensitive_kg=True` 传进来，把健康/用药三元组拉进 `[MEMORY]`。LiveKit 那条路不传（默认 False）。这一条值得你定：这是有意的，还是上一轮拆分时漏掉的一个口子？

---

## 9. 与 mempalace 的关系

**我们一次都没用他们的图。** `grep "mempalace.knowledge_graph"` 在 `eidolon/` 里零命中。我们用的是 `mempalace.palace_graph`（房间图，从 Chroma 元数据算的 wing/room/hall），跟 KG 无关。

对比（关键行）：

| | mempalace | 我们 |
|---|---|---|
| space_id | 无（一文件一租户） | 每表每索引每 WHERE |
| audience / sensitive | **完全没有** | 两个列，SQL 内过滤 |
| 时间戳 | 原样存，比较时 `CASE` 加宽（毁索引） | 写入归一，谓词是普通 SQL |
| 实体消解 | 只有 slug | slug + alias 表 + 前缀尾匹配 |
| 幂等 | 只查未结束的同三元组 | **先查 source_turn_id 重放**，再查未结束 |
| 多 subject 召回 | 做不到，一个实体一次调用 | `ROW_NUMBER() OVER (PARTITION BY subject_id)` 单语句 |
| 并发 | 一把 `threading.Lock` 互斥 | reader/writer |

**他们有而我们没有的**（三件，都不大）：`entities.type`/`properties` 真的被写、`query_relationship(predicate)` 按谓词查、以及 **`add_triple` 会拒绝 `valid_to < valid_from`**（`knowledge_graph.py:278-286`）——我们不校验区间顺序，能写出一条对任何 as-of 查询都不可见的陈述。这算我们的一个真缺口。

### 架构文档那句话：结论对，理由不准

`docs/ARCHITECTURE.md:78` 说"mempalace 的图层硬编码 `import sqlite3`，我们只能自写"。

- **真的**：`import sqlite3` 在模块级，`sqlite3.connect` 在类里直接调，没有 backend 概念、没有 registry、没有 Protocol。他们的**向量**存储有完整的 backend 体系（chroma/milvus/pgvector/qdrant），图被刻意排除在外。
- **不准**：**路径不是硬编码的**——`db_path` 是构造参数，两个真实调用方都传了。文档那句话字面指向了唯一可配置的那个东西。
- **更强的理由**：audience 和 sensitive 在他们 schema 里根本不存在，`space_id` 也不存在。**缺列不是依赖注入能解决的问题。**

建议把那句改成「图层没有 backend 概念，schema 里也没有 audience 和 sensitive」。

### 还有一个更早的问题：当初的第一条理由已经作废

`c90c08b` 的开头写着自研图是因为"一旦多主机服务一个 space，图是唯一不能留在本地磁盘的部分——它既是共享存储的阻塞点，也是最该甩掉的依赖"。`729ec17` 为此加了 PostgreSQL 图，`3fc70e0` 在 local-only 决定下把它删了。**"共享存储的阻塞点"已经不存在了。** 剩下的理由（audience 成列、时间戳归一、按 subject 分预算）依然成立。

连带地：`kg_sql.py` 的开篇仍写着"两个存储实现共享"、"方言差异是参数标记和 upsert 子句"，但现在只有一个实现，upsert 子句随 `kg_postgres.py` 一起删了，`audience_filter(count, marker)` 的 marker 参数只有一个调用方传 `"?"`。`docs/TEST_REPORT.md:276` 也还写着 `KnowledgeGraphPort | 两个实现`。

---

## 10. 缺陷清单，按严重度

### P0 — 现在就是坏的

**1. `eidolon_memory_kg_snapshot` 调不通** ✅ 我实际调过：

```
eidolon_memory_kg_snapshot   ToolError: SqliteKnowledgeGraph.timeline() missing 1 required
                                        keyword-only argument: 'audiences'
eidolon_memory_kg_timeline   OK
```

`mcp_server.py:1051-1055` 没传 `audiences`，而它在 port 和实现里都是**无默认值的 keyword-only**。同一个文件三个函数之上的 `kg_timeline` 传对了。**没有任何单元测试覆盖它**——`test_kg_mcp_gateway.py` 的工具名断言里就没有它。`scripts/bench_mempalace_full_ab.py:479` 同样的错。

顺带：`current_only` 是在 SQL `LIMIT` **之后**用 Python 过滤的，所以已结束陈述多的图会返回远少于 `max_triples` 的当前条目，而 `"capped"` 又是拿过滤后的条数算的。

**2. `mempalace repair` 会丢掉六个 ledger** ✅ 逐行确认：

`repair.py` 把整个 palace 目录 `os.rename` 成 `<palace>.pre-rebuild-<ts>`，`os.makedirs` 一个**空**目录，然后只调 `_preserve_knowledge_graph_sqlite`（按**文件名**拷 `knowledge_graph.sqlite3{,-wal,-shm}`，不打开库）。

我们往 palace 里放 7 个 sqlite：`canonical_facts`、`command_status`、`commitments`、`dlq`、`extraction_decisions`、`sync_ledger`、`knowledge_graph`。**回来的只有 `knowledge_graph` 和重建的 chroma；另外六个留在归档目录里，永不恢复。**

而 `history_reset.clear_repair_archives` 会 `shutil.rmtree` 掉 `*.pre-rebuild-*`——所以那六个 ledger 的唯一副本会在下一次 history reset 时被删掉。

丢掉 `canonical_facts` 意味着失效链断掉（`projection_id` 只存在那里），丢掉 `commitments` 意味着"你答应过我什么"返回空。这两个按 ARCHITECTURE 的说法是产品行为而不是记账。

我们的图之所以能活下来，纯粹是因为**文件名和他们的一样**——一个没人 pin 也没人测的上游常量。

### P1 — 静默的错

**3. `recall.kg_window_days` 是死配置** ✅（见 §7）
**4. `palace_inventory.py:15` 用的是旧表名** ✅ —— `("entities","triples","entity_mentions")`，我们现在是 `kg_entities`/`kg_statements`/`kg_entity_mentions`。有 `if table in available` 守着所以不报错，**静默把每个图的 counts 报成 `{}`**。更糟的是那三个名字正好是 **mempalace 的**表名。
**5. 没有旧 schema 的迁移**。`_initialise` 只有 `CREATE TABLE IF NOT EXISTS`，打开一个 `c90c08b` 之前的文件会在旧表旁边建空的 `kg_*`，旧图静默不可见。（`~/.eidolon-trash/` 下的都是旧 schema；需要确认没有活的 palace 早于那个 commit。）
**6. `record_entity_mention` 的 `entity_id` 命名空间不一致**。它是 port 上唯一不对入参跑 `entity_id_for()` 的写方法；测试传 slug，生产传 steward 的原始名（`turn_processor.py:660`）。`mother:张丽` 恰好一致，`"My Dad"` 就永远 join 不上，alias 静默失效。

### P2 — 设计上的松动

**7. `supersede` 有实现、有"为什么不能拆成两步"的文档、零生产调用者**。生产的单槽更新恰恰是它警告的那种：`invalidate` 一次锁、`add_triple` 另一次锁，中间有个窗口读者看不到这个事实。
**8. `should_write=False` 仍写图** ✅
**9. triples 无上限**
**10. `RECALL_TOTAL` 每次 MCP 召回加两次**，而且第二次 `degraded` 恒为 `"false"`。
**11. `GRAPH_TIMEOUTS` 从超时值反推 kind**（`<=0.1` 算 voice），调用方明明知道 `for_voice` 却没传。
**12. canonical invalidation 的升级条件耦合了不相关的 ledger** —— 只有 `decision_store is not None` 时才记失败，否则悄悄降级成 log+ack，和声明的失败模型矛盾。
**13. 一个测试断言和自己的名字相反**：`test_a_bare_prefix_matches_nothing` 实际断言 `== ["pet:"]`，即匹配上了。
**14. `query_entity` 没有 LIMIT**，`query_entity_combined` 在全量结果跨过线程边界之后才切片。
**15. 我们不校验 `valid_to >= valid_from`**（他们校验）。

### P3 — 死代码
`KgEntityRecord`（导出、零消费者）、`entity_type`/`properties`/`mentions.confidence` 三个空转列、`PredicateTemporality`、`kg_sql.py` 的方言参数、`domain/kg.py:23` 的空标题。

---

## 11. 需要你拍板的

1. **P0 的两个先修掉？** `kg_snapshot` 是一行；`repair` 丢 ledger 我建议在**我们的** supervisor 里做恢复（归档路径是确定的，`supervisor.py:427-428` 已经有"谁保住了什么"的模型），而不是等 mempalace 长出第二个 `_preserve_*`。
2. **`recall_context` 的 `include_sensitive_kg` 要不要从 agent 面拿掉？** 现在对话模型能自己开健康三元组。
3. **`kg_window_days` 是实现它还是删掉它？** 30 天窗口对伴侣记忆是不是想要的行为——"你三年前说过喜欢乌龙茶"该不该召回。
4. **`kg_sql.py` 的方言参数**：只剩一个实现了，和 embedder 那次相反——这次可能该收掉，因为没有第二个图存储在排队。
5. **audience 写入归层**（ARCHITECTURE 的未完成项）要不要现在做。它是"两层可见性"从惰性变成真实生效的前提，但需要 steward 逐条判断。
6. **`query_entity_combined` 的 N 次往返**要不要换成单语句——它是默认召回路径，跑在 50ms 预算里。
