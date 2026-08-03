# 架构与实际进度

写于 2026-08-03。文中每个数字都是在本分支上实测得到的，不是凭记忆写的。

先读这份再读代码。它回答两个问题：这个服务是什么形状，以及每一部分做到了哪里。

---

## 一个贯穿全部的想法

**memory space 是参数，不是进程的身份。**

其余一切都由此推出。原来进程**就是**一个 space：启动时解析句柄、端口由 space id
派生、一把 flock。这让常驻的 embedding 模型变成 per-space 成本——"一个 owner
三个 companion"要三个进程、三份 300MB 模型。

现在调用方传自己的 context，router 解析那个 space 的句柄，服务作答。一个进程持有
几个 space 是部署决策。

---

## 分层

| 层 | 文件 | 行数 | 负责 | 不允许 |
|---|---|---|---|---|
| `contracts/`（独立包） | 12 | 1230 | wire 与服务契约，仅依赖 pydantic | 知道任何存储的事 |
| `domain/` | 20 | 2552 | Port、模型、纯决策 | 做 I/O |
| `application/` | 26 | 5937 | 召回、turn 处理、服务对象 | import 存储库，或比较 backend 的名字 |
| `adapters/` | 15 | 4172 | 向量存储、图、三个 router | — |
| `infrastructure/` | 22 | 6126 | ledger、NATS、palace 引导 | — |
| `entrypoints/` | 7 | 3692 | 进程装配：MCP、订阅者、supervisor | 持有业务逻辑 |
| `config/` | 6 | 1158 | 配置、registry | import adapters |
| `support/` | 5 | 334 | metrics、tracing、日志 | — |

其中两条边界由**测试强制**而非约定（`test_layering.py`）：逻辑层不得 import
mempalace，不得比较 backend 名字。这两条在写出来时都抓到了真实违规。

---

## Port 与它们的实现

每个 Port 在 `domain/`；每个实现以它对话的对象命名。25 个 Protocol，全部有消费者。

```
MemorySpaceRouter ─── LocalPalaceRouter      嵌入式句柄池，每 space 一把 flock
                  ├── SharedStoreRouter      无状态，任何副本服务任何 space
                  └── FixedSpaceRouter       句柄在别处打开，只服务一个 space

VectorStorePort ───── MemPalacePythonBackend  唯一真实现；chroma↔milvus 的切换
                  │                           发生在 mempalace 内部（经 env）
                  ├── LockedBackend           加 per-space 锁的包装器
                  └── FakeMemoryBackend       测试用

KnowledgeGraphPort ── SqliteKnowledgeGraph   ┐ 共享 kg_sql.py：schema 与
                  └── PostgresKnowledgeGraph ┘ 查询形状，一处定义

WarmableBackend      能力协议——存储自己回答能不能预热
RoomGraphBackend     能力协议——存储自己回答能不能枚举房间

6 × ledger ports ──── SQLite（palace 内）    ┐ 共享 ledger_sql.py
                  └── PostgreSQL（共享库）    ┘ 6 个里完成 5 个
```

**一个值得知道的不对称**：图和 ledger 各有两个我们自己拥有的实现。向量只有一个——
chroma↔milvus 的切换在 mempalace **内部**发生。所以 milvus 路径出问题，我们能验证
但不能修。这是 mempalace 的向量层有 backend registry、图层没有所导致的后果，不是我们
的选择。

---

## 读路径全程

```
agent ──MCP──▶ mcp_server 工具 ──▶ MemoryService.recall_fused(ctx, …)
                                        │
                                        ├─ router.resolve(ctx.memory_realm_id)
                                        │     └─▶ 该 space 的 backend / kg / ledgers
                                        │
                                        └─ recall_with_kg_fusion
                                              ├─ 跨 wing 向量检索
                                              ├─ 图查询（有界、可选）
                                              ├─ 主题通道
                                              └─ 召回策略：space、设备、audience
```

27 个 MCP 工具里目前有 2 个走服务层：`search` 和 `recall_context`——也就是 agent
唯一读的那两个。其余 25 个面向运维，仍是单 space，这符合它们的语义。

`search` 与 `recall_context` 回答的是不同问题，故意不走同一条路：search 是"你记得
关于这个的什么"，recall 是"这一轮什么相关"（所以才有图融合、近期轮次、会话过滤）。

---

## 两层可见性

```
audience = "owner"           关于 owner 本人的事实——所有 companion 都可召回
audience = "companion:<id>"  与那一个 companion 之间发生的——对它私有
```

在图的查询里和向量存储的可见性 gate 里强制，两侧都以 owner 层为默认。来源
（`companion_id`）与可见性（`audience`）是**分开的两个字段**——混在一起会让每条记忆
意外变成私有。

**这条轴目前在生产中是惰性的**，原因是结构性的：今天一个 space 是
`(owner, companion)`，所以每个 companion 是独立的库。没有可泄漏的，也没有可共享的。
它在 space 变成 owner 之后才真正生效，而那正是下面 1:N 那项工作所解锁的。

---

## 两种部署形态

一套代码。形态**由存储位置推导**——没有 `mode: local|cloud` 开关，因为开关可能与存储
配置矛盾。

| | 本地 | 云端 |
|---|---|---|
| 向量 | chroma，palace 内文件 | milvus 服务端 |
| 图 | palace 内 SQLite | PostgreSQL |
| ledger | palace 内 SQLite | PostgreSQL（6 个里 5 个） |
| router | `LocalPalaceRouter`——每 space 一把 flock | `SharedStoreRouter`——无锁、不派生端口 |
| turn ring | 进程内 | 不存在（否则每副本不同） |
| 连接池 | 不适用 | **每副本一个**，图与所有 space 的所有 ledger 共享 |
| 进程 : space | **今天 1 : 1**，最后几步落地后 1 : N | M 副本 : 全部 space |

唯一的不对称由测试断言：嵌入式存储拒绝第二个持有者；共享存储允许两个副本并发服务同一
个 space。

---

## 进度

### 已完成并验证

| | 证据 |
|---|---|
| 脱离 Eidolon OS 独立 | 核心代码不 import 任何 `eidolon_*` 包——由两个守护测试强制（静态 AST 扫描，加一个屏蔽 OS 包后加载所有 entrypoint 的子进程）。contracts 的 46 个测试在只装 pydantic 时通过。**精确说**：核心依赖里唯一的 `eidolon-*` 是 `eidolon-memory-contracts`，那是本仓自己的包（`path = "./contracts"`，仅依赖 pydantic）；`eidolon-data` 只出现在可选的 `eidolon-os` extra 和 dev 里 |
| 契约包自持 | 12 文件 1230 行，仅依赖 pydantic |
| `MemoryReadContract` 已实现 | 8 个方法全部在 `MemoryService` 上；契约**就是**服务本身 |
| 向量 chroma ↔ milvus 靠配置切换 | 对真机验证过（8.140.214.42，`eidolon` 库） |
| 自有图，两种方言 | 41 个 SQLite 测试 + 11 个对真实 PostgreSQL |
| PostgreSQL 变得可测 | `pgserver` 以 wheel 分发二进制；11 个从未运行过的测试现在进了常规套件 |
| ledger 上共享存储 | **6 个里 5 个**——decisions、sync、dlq、command_status、commitments |
| 一份状态机而不是两份 | commitment 的决策抽成两个存储共同调用的纯函数；查源码断言，所以副本无法悄悄回来 |
| ledger 写入有界 | 每 ledger 在 event loop 里串行化，外加一个低于线程池规模的进程级上限 |
| 两层可见性 | 在图查询和向量可见性 gate 里强制 |
| 可观测性 | prometheus `/metrics` 挂在既有端口；contextvar span，字段用 OTel 命名 |

**900 个单元/契约测试通过，6 skipped。e2e 25 passed / 2 failed**（那 2 个是既有的
LLM 抽取缺陷——见 TEST_REPORT.md）。

### 未完成，附真实阻塞

| | 状态 | 阻塞 |
|---|---|---|
| `canonical_facts` 上 PostgreSQL | **唯一缺失的 ledger**。schema 已共享，SQLite 侧正常工作 | 12 个方法约 1000 行要翻译。现在是机械工作，但量不小 |
| 进程 : space = 1 : N | router、服务、ledger 边界全都支持了。`agent_runner.py` 仍传 `allowed_spaces=[one]` | 3 个 handler 接固定句柄，必须改为接 router——**47 个测试调用点**，格式混杂，无法安全脚本化 |
| NATS 一个 consumer 服务所有 space | 通配 subject 辅助函数已存在；`turn_processor` 本来就从 payload 取 space | 同样是那 47 个调用点 |
| 单端点 / discovery | | supervisor 掌管进程拓扑——**按你的指示暂缓** |
| 收窄 MCP 响应 | `RecallResult` 按设计不含 `kg_triples` | agent 的 `port_adapter.py:201` 在读它——需要两个仓库同批 |
| 写入侧 audience 归层 | 目前全是 owner 层 | 需要 steward 逐条判断 |
| 四个公开 benchmark | 口径已对齐、探针已跑、脚手架已规划 | **抽取覆盖率：40 轮产生 7 个 fragment。** R@5 不可能超过它，所以今天发数字等于在测一个已知缺陷 |

### "云端少一个 ledger"具体损失什么

`SharedStoreRouter` 对 `canonical_facts` 交出 `None`，而每个消费者已有的 `None`
容忍让服务照常运行。所以一个共享存储的部署会启动、会服务召回，**但不会失效已被纠正的
事实**——让被更正的记忆停止被召回的那条链就在这个 ledger 里。

这一点写在 router 的启动日志和 `config/settings.cloud.example.yaml` 里，所以运维会比
用户先遇到它。

---

## 目前最重要的一个数字

**抽取覆盖率：40 轮对话产生 7 个 fragment（17.5%）。**

MemPalace 把每个 session 原样存下，所以它公布的 96.6% R@5 测的是"在全部内容上检索得
多准"。我们测出来的会是"在 steward 选择保留的六分之一上检索得多准"。同一次探针的分类
明细：emotion 3/3、time 3/5、**preference 0/4**、**abstention 0/5**。

preference 是伴侣记忆最核心要回答的东西。延迟不是问题——端到端过 MCP 的 p95 是
104ms。

改进检索器几乎不会让这些数字动。这就是 benchmark 顺序必须是：先修抽取，重跑探针，再
发布数字。
