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

`find <层> -name '*.py' | wc -l` 与 `-exec cat {} + | wc -l`，含子包。上一版这张表漏了
子包（`infrastructure/nats/` 之类），所以几个数字偏小。

| 层 | 文件 | 行数 | 负责 | 不允许 |
|---|---|---|---|---|
| `contracts/`（独立包） | 12 | 1230 | wire 与服务契约，仅依赖 pydantic | 知道任何存储的事 |
| `domain/` | 21 | 2857 | Port、模型、纯决策 | 做 I/O |
| `application/` | 32 | 6742 | 召回、turn 处理、服务对象 | import 存储库，或比较 backend 的名字 |
| `adapters/` | 13 | 3318 | 向量存储、图、router | — |
| `infrastructure/` | 32 | 6587 | ledger、NATS、embedder、palace 引导 | — |
| `entrypoints/` | 7 | 3698 | 进程装配：MCP、订阅者、supervisor | 持有业务逻辑 |
| `config/` | 6 | 1401 | 配置、registry | import adapters |
| `support/` | 5 | 354 | metrics、tracing、日志 | — |

**`infrastructure/` 不得 import `adapters/`**，这条由测试强制，而且是被真实故障教出来的：
`adapters/__init__.py` eager import `mempalace_python_backend`，后者 import 回
`infrastructure/mempalace_backend`。所以 infrastructure 里一行模块级反向 import 就经包的
`__init__` 闭合成环——我把 embedder 放进 adapters 时正是这样，症状不是启动报错，而是建
palace 的子进程抛 `PalaceInitError`。Port 实现住在 infrastructure 本来就是既有约定：
六个 ledger 都在那里。

另两条边界同样由测试强制（`test_layering.py`）：逻辑层不得 import mempalace，不得比较
backend 名字。两条在写出来时都抓到了真实违规。

---

## Port 与它们的实现

每个 Port 在 `domain/`；每个实现以它对话的对象命名。

```
MemorySpaceRouter ─── LocalPalaceRouter      句柄池，每 space 一把 flock
                  └── FixedSpaceRouter       句柄在别处打开时的包装器，只服务一个 space

VectorStorePort ───── MemPalacePythonBackend  chroma
                  ├── LockedBackend           加 per-space 锁的包装器
                  └── FakeMemoryBackend       测试用

KnowledgeGraphPort ── SqliteKnowledgeGraph    schema 与查询在 kg_sql.py，一处定义
                                              （单实现；方言参数已随 PG 图一起删掉）

EmbeddingPort ─────── OnnxSentenceEmbedder    进程内 ONNX,8 个模型,默认 bge-small-zh
                  ├── HttpEmbedder            OpenAI 兼容的 /v1/embeddings
                  └── MemPalaceEmbedder       把 mempalace 自己那两个包成同一个形状

                     MemPalace 3.8 public API 显式接收 document/query vectors

WarmableBackend      能力协议——存储自己回答能不能预热
RoomGraphBackend     能力协议——存储自己回答能不能枚举房间

6 × ledger ports ──── SQLite（<space>.ledgers/） 语句在 ledger_sql.py 一处定义
```

**为什么大部分 Port 只有一个实现，抽象层还留着**：这些 Port 不是为了"将来换实现"存在的。
`MemorySpaceRouter` 是把 space 从进程身份变回参数的那个东西——没有它，一个进程只能服务
一个 space。

`KnowledgeGraphPort` 存在的理由，2026-08-29 按 mempalace 3.8.0 的源码重新核过一遍，
**结论不变但其中一条当时说错了**：

- **成立**：他们的 schema **没有** `space_id`、`audience`、`sensitive`。我们是多租户加两层
  可见性，这三列要进每一个 WHERE。**缺列不是依赖注入能解决的问题。**
- **成立**：他们的图层没有 backend 概念——`import sqlite3` 在模块级，`sqlite3.connect` 在
  类里直接调，没有 registry、没有 Protocol，而他们的**向量**存储有完整的 backend 体系。
- **成立**：date-only 的 `valid_to` 是每次比较时用长度判断加宽的，不是写入时归一——那会毁
  掉索引。我们在写入时归一，于是区间判断是普通 SQL。
- **不成立，原文写错了**：说他们"把路径写死成 `~/.mempalace/knowledge_graph.sqlite3`"。
  那是 `db_path=None` 时的默认值，**`db_path` 是构造参数**，两个真实调用方都传了。这句话
  字面指向了他们那个图里唯一可配置的东西。

还有一条更根本的，当时没写：**他们的图默认是空的。** 抽取管线（`convo_miner` /
`general_extractor` / `dedup`）一行都不写它，唯一的写入口是 MCP 工具 `kg_add`，
`searcher.py` 对它零引用——**图不在他们的检索路径上**，`seed_from_entity_facts` 零调用者。
所以那是一个由 agent 手动维护的旁路设施，不是一个被填充、被使用的图。详见
`MEMORY_FUSION_PLAN.md` §1。

**这个 port 服务的不是"换 backend"**（`Literal["none","sqlite"]`，只有一个实现），而是
关闭开关的契约、可测性、以及说清楚图欠召回什么。

每一个都在解决一个当下的问题，不是占位。

`EmbeddingPort` 是这里唯一有多个实现的：它先是因为 mempalace 用 if/else 选 embedder、
没有注册点而存在——选择权必须在我们这边——现在**换实现只是一行配置**
（`embedding.provider`）。而且一个实现的 port 是没被验证过的 port：只有一个 ONNX 实现时，
它的每个假设读起来都像契约的一部分，没有东西说明哪些是。第二个实现落地时暴露的正是这类
东西，下面那一节记着。

由测试强制的两条边界（`test_layering.py`）：逻辑层不得 import mempalace，不得比较
backend 的名字。两条在写出来时都抓到了真实违规。

---

## ledger 是什么

字面是**账本**。`canonical_facts` 是长期事实的事实源：assertion、evidence、audience 和生命周期
先写入这里；Chroma drawer 与 temporal KG 都是带 assertion/evidence/projection identity 的
可补偿投影。其余 ledger 记录抽取、命令、同步和承诺状态。不存在“自然 turn 的 Chroma 事实”
与“显式 fact 的 ledger 事实”两套互相竞争的真相。

6 个 ledger，共 **13 张表**（2026-09-02 从 `ledger_sql.py` 与 `extraction_decisions.py` 的 `CREATE TABLE` 实测）：

| ledger | 表 | 存什么 | 丢了会怎样 |
|---|---|---|---|
| `extraction_decisions` | 2 | steward 对每个 turn 的抽取结论，加隐私 tombstone | 重放同一 turn 会**再问一次模型**；tombstone 丢了则**删过的 source event 可被重放复活** |
| `sync_events` | 1 | 哪些离线批次已应用 | 设备重连时**重放已写入的 turn** |
| `dlq_entries` | 1 | 处理失败的 turn 原文 | 失败的 turn **无从查看、无从重放** |
| `command_status` | 1 | 异步命令到了哪一步 | 查不到写入结果。**这一个是投影，可重建** |
| **`commitments`** | 3 | 承诺 + 不可变修订史 + 隐私表 | **承诺查询返回空** |
| **`canonical_facts`** | 5 | 已确认事实 + 证据 + 失效 + 重新激活 + 遗忘 | **纠正过的事实继续被召回**；遗忘表丢了则**硬删除无法收敛** |

### 为什么后两个是产品行为而不是记账

失效链的实际机制（`application/canonical_invalidation.py`）：

```
用户说"我现在不喝乌龙茶了"
  ↓
register_invalidation()              ← ledger 记下失效请求
  ↓ 若 state == "applied" 则早返回     ← 幂等守卫，防止重复归档
  ↓
用 assertion_id / projection_id 定位所有投影
  ↓
archive_many()   ← 归档它，从此不再被召回
kg.invalidate()  ← 图里的三元组同时失效
  ↓
mark_invalidated()                   ← 两个投影完成后 ledger 标记完成
```

**`projection_id` 是"该归档哪一条"的唯一线索**，它只存在于这个 ledger 里。所以
`canonical_facts` 不可用时事实写入和删除都 fail closed。隐私删除同样先写 tombstone/outbox，
再按 assertion_id 删除 KG 和 drawer；硬删除会抹去 ledger 的明文 evidence，并保留不含事实内容
的 opaque tombstone 阻止备份或旧事件重放复活。

`commitments` 同理：它是 `eidolon_memory_commitments` 这个对外工具的唯一数据源。没有它，
"你答应过我什么"的回答是空的，而不是"我不知道"。

### 为什么和向量/图分开

三个理由，都不是审美：

1. **锁**。向量库（chroma）与图共享一把 per-space 锁，因为一个 turn 要原子地写两者。
   ledger 不参与那个临界区——命令状态的读不该排在 chroma 写的后面。
2. **重建性不同**。`command_status` 是投影，丢了只影响诊断（它自己的文档写明：丢失终态
   会让命令重新显示为 accepted，但**永不会**让未应用的显示为成功）。而 `dlq_entries` 和
   `sync_events` 丢了就是丢数据。这个区别直接决定了 schema 守卫的行为——前者空表重建，
   后者指名拒绝。
3. **行为定义在 Port 上而不是文件上**。契约测试断言的是"重放的 turn 被识别""claim
   不会交给两个 worker""已应用的命令不被降级"，没有一个碰 SQLite。这曾用于证明两种存储
   等价；现在只有一种存储，但这个性质留下了——夹在 SQL 语句里的状态机是没有数据库就测
   不了的状态机。

### 写入的两道界

ledger 的写不像 chroma 那样共享 palace 锁，所以它们有自己的两道界，各做不同的事：

- **每个 ledger 一把 `asyncio.Lock`**：让同一个文件的写在 event loop 里排队，而不是在
  SQLite 里撞上 `busy_timeout` 干等最多 5 秒——那期间会占住一个线程池 worker。
- **一个进程级信号量**：上限设为 CPU 核心数，低于线程池规模。实测这台机器 12 核 → 线程池
  16 worker，而 6 ledger × 3 space = 18，**三个 space 就会耗尽**，之后向量存储和 embedding
  会排在记账后面。有界而非串行化：多个 space 仍能同时跑，只是拿不走整个池。

router 是句柄的唯一来源，这一点由测试强制（`test_layering.py`）：entrypoint 里不得构造
ledger。曾有一段时间 sync ledger 被构造两次——router 一个、订阅循环一个——同一文件两把写
锁，串行化在两者之间不生效；当时无害仅因为 router 那个恰好没有消费者。

### 三条写路径规则，以及我们违反了哪几条

2026-09-02 读 MemPalace 3.8 源码时发现，上游把三条规则写在了代码注释里。它们不是风格偏好，
每一条都对着一个具体的失效模式。我们对前两条是相反的，第三条我们没有。

**规则一：写路径永不丢一轮。** `convo_miner.file_conversation_exchange` 在 wing 名字非法时
降级到 `wing_general` 而不是报错，理由原文是 *"dropping a turn over a config typo would break
the verbatim / 100%-recall promise"*。

我们相反。到 2026-09-02 为止，同一形状的缺陷修了四个——`memory_space_id`、
`source_turn_id`、`occurred_at`、`evidence_quote`——都是「一个非实质字段为空 → 整轮抽取作废
并重投」。前两个的教训在 `steward/llm.py` 的 docstring 里记过，之后还是又出了两次。

**根因不是这四个字段，是这条规则从未被表述。** 每次都靠实测撞出来再打补丁。而且这个类不止
在 Memory：同一天另一条线在 setup descriptor 的可选字段 `device_base_id` 上发现了第五个实例
（缺失接受、空串让整份 descriptor 作废，Dart 绑定同形状），目前生产方够不到，方向已定为「空
等于没有，消费方按缺失处理」。**所以它是工作区级的缺陷类，不是本仓局部问题。**

结构上的根因是校验顺序：身份/来源字段的填充（`stamp_fragment_identity`）跑在校验**之后**，
所以 `_parse_decision` 只能逐个把字段「取回」。填充如果在校验之前，这一类不可能存在。

**规则二：LLM 是可选的，而且在后面。** `closet_llm.py` 原文：*"Regex closets are always created
by the miner; this path regenerates them **afterward**. Core memory operations remain API-free
by design."*

我们的 steward 是必需的、而且在最前面。后果是可测的：一轮对话的可读时间 17–75 秒，其中
steward 占 99.4–99.9%；steward 失败等于这一轮没有记忆。

还有一处具体的代码问题：`process_turn_message` 的失效模型注释说「projection 失败 → NAK /
DLQ，重投恢复未完成的投影」。这对**投影**是对的——重放投影很便宜。但同一个失效模型被套用在
**抽取**上，而抽取是全系统最贵的单次操作。代码把两者当同一件事，所以一次字段校验失败要重跑
整个 17–75 秒的抽取。

**规则三：verbatim 是地板，结构只是排序信号。** `searcher.py` 原文：*"Closets are a ranking
**signal**, never a gate, so weak closets can only help, **never hide drawers the direct path
would have found**."*

**2026-09-02 之前我们没有地板**，现在有了，见下。

### 原文证据层

`application/verbatim.py`。收到一轮对话时，把**用户说的那句原文**在 steward 之前无条件落库；
steward 照旧异步在其上叠加结构。

**为什么加**：同一套 48 查询上，蒸馏答 25–28/43，仅用户原文答 32/43，**各有 6 条是对方拿不到
的**——原文答「说过什么」（代词、模糊指代、`上周聊了什么`），蒸馏答「这意味着什么」
（`我答应了什么`，用户从没这么说过）。而且在此之前，steward 当场漏掉的东西**永远不存在**：
决策账本只存 `input_hash` 不存原文，没有第二次机会。41.7% 因此不是检索得分，是一次性抽取的
天花板。

**为什么有界**：规模是实测的，用 CLongEval 的 8379 条真实中文陪伴式话语稀释（同语言同体裁）：

| 库中抽屉 | 命中/43 | p95 |
|---|---|---|
| 40 | 32 (74.4%) | 15.1ms |
| 540 | 29 (67.4%) | 14.5ms |
| 2040 | 28 (65.1%) | 20.2ms |
| 8040 | 27 (62.8%) | 40.3ms |

退化次线性，200 倍体量仍落在蒸馏于 40 抽屉时的区间内。但曲线形状正是保留窗不可省的理由：
**原文的价值随体量衰减，成本随体量增长**，全存等于在最不需要的地方花最多的钱。

**三个不重复造轮子的决定**：

- 保留形状照抄 `CommandStatusConfig`（`retention_days` + `max_records` +
  `prune_every_writes`，写路径每 N 次触发）。consolidator 本是清扫的显然宿主，也是错的——
  它是 opt-in 的被监管子进程，**生产里没在跑**，挂上去等于又造一个不会被调用的机制。
- 删除复用 `forget` 的 `source_event_id` 解析：抽屉带这个标签，现有隐私路径已经覆盖它，
  不需要第二条删除路。
- 可见性是 `session` / `current_device`。scope 取决于内容、由 steward 决定，而写入时
  steward 还没跑；两种猜法不对称——猜 `all_devices` 会把「这台设备在客厅，麦克风需要校准」
  泄漏到所有设备（一个 multidevice E2E 当场抓到），猜 `current_device` 只是少服务。这也是更
  诚实的描述：跨设备的是蒸馏出的事实，原文是这次交互自己的记录。

**这一层默认开启（`worker.verbatim_retention_days = 180`）**，但它先被实测否掉过一次，
三个缺陷修掉后才打开。

写入侧是对的、有界的、有测试的。但把两层放进同一个 palace、走真实召回路径实测：

| | 43 条可答查询 |
|---|---|
| 仅蒸馏 | 25 |
| 蒸馏 + 原文 | **13** |

**丢了 12 条，一条没赢。** 那个 84–86% 的并集是把两层**分别**测出来在纸上取并，而召回只有
一个 `top_k` 预算、两层共享它。

三个原因，全部查清并修掉：

1. **抽屉身份是 `(space, room)`**（`_doc_id(space, room)`）。canonical 抽屉用
   `room=f"fact_{predicate}_{projection_id}"`，每条唯一 —— 正是为了这个。原文层用了固定的
   `room="conversation"`，于是每一轮覆盖上一轮。
2. **可见性是后置过滤**：`recall_policy` 在按 wing 取回 `n_results=top_k` 之后才判
   `current_device`，所以设备域的行先占名额再被丢弃。
3. **设成 `current_device` 却没盖 `source_device_id`**。可见性规则是
   `context.device_id ∈ {source_device, target_device}`，两者都空则永远为假——这些抽屉
   **对任何调用方永远不可见，同时照样占取回窗**。这就是先前"未解释"的那条。

修完重测：调用方带 `device_id` 时 **25 → 29（+4）**，不带时 25 → 25（+0，原来是 13）。

第 3 条的修法是把设备可见性谓词像 `audience` 一样推进查询（`_device_visibility_filter`），
只做减法。**这条独立于原文层也该修**——steward 本来就给设备本地事实写 `current_device`，
读路径今天已经有这个饥饿，只是规模小。

`test_the_shipped_default_keeps_the_layer_on` 钉住这个决定，让关掉它也必须是刻意的。

---

## embedder：公开接口与模型契约

Eidolon 只拥有 BGE-small-zh 的 document/query 向量生成服务；不再向 MemPalace 注入私有
embedder、embedding function 或 cache。MemPalace 3.8 的公开 `BaseCollection.upsert` 与
`query(query_embeddings=...)` 接收显式 512 维向量，collection identity 使用官方
`openai-compat` provider 配置。若公开接口不再满足 identity、维度或前缀契约，启动直接失败，
不增加私有 workaround。

MemPalace 3.8 的公开 collection API 接收显式 document/query vectors，并提供公开的
OpenAI-compatible provider；Eidolon 不再写入它的私有 provider/cache。

### 抽象层与实现层，以及那条缝在哪里

```
domain/embedding_port.py          EmbeddingPort、EmbedderIdentity、EmbeddingError
                                  只有 identity() / embed_documents / embed_queries
        │
        ├── infrastructure/onnx_sentence_embedder.py    进程内 ONNX
        ├── infrastructure/http_embedder.py             OpenAI 兼容 /v1/embeddings
        └── infrastructure/mempalace_embedder.py        mempalace 自己那两个
        │
        └── infrastructure/embedder_factory.py          配置 → 实现,唯一一处
```

**Port 里没有 Chroma 的形状。** Adapter 通过 MemPalace 3.8 的公开 `embeddings` 与
`query_embeddings` 参数传入向量；MemPalace 的 `openai-compat` 只负责 collection 的公开
provider/identity 契约。读写路径不再依赖私有 `_EF_CACHE` 或 provider 解析函数。

**换实现只改 `embedding.provider`**，别的什么都不动。已经真跑过一遍：对着一个 OpenAI 兼容
端点端到端建出 palace，chroma 把声明的宽度持久化下来，再用 `provider: local` 去读，被
`EmbedderIdentityMismatchError` 挡住并同时报出两个 embedder 的名字。

**第二个实现逼出了三件之前不成立的事**，这是"一个实现的缝没被验证过"的具体内容：

1. **embedding 会失败。** 只有进程内 ONNX 时它基本不会——没有超时可撞、没有半截响应要对
   齐、没有需要调用方先声明的宽度。所以没有一个调用方有错误路径。`EmbeddingError` 现在在
   port 上，"向量没到"不能变成空向量：写入侧那是一条永远召回不到的 fragment，读取侧那是
   "没有相似的"，读起来像空记忆而不像失败的调用。
2. **响应要按 index 放，不能按位置。** API 给每行编号且不承诺顺序。内部再分批并重排的
   provider 会让每个 fragment 配上另一个 fragment 的向量，而下游没有任何东西能比出来。
3. **宽度必须声明，然后逐条校验。** 它在 collection 创建时定死，所以不能靠问网络服务；声明
   错了会在 chroma 那里变成一次被拒的写入，读起来像存储故障而不像配置错误。

**配置搬到了 `embedding:` 一节，有自己的校验器。** embedder 已经不是 mempalace 的了。
`mempalace.embedding_*` 四个键仍然能用，在校验**之前**折进新的一节——所以写在旧地址的错模型
名依然在加载时报错，而不是绕过校验。两处都写且不一致时拒绝，而不是按优先级选一个：不管选哪
条规则，都有一半的读者是对的，而从文件里看不出是哪一半。
新一节验证完之后会**反向写回**那四个旧键，因为 `entrypoints/supervisor.py:376` 用
`mempalace.embedding_model` 拼 `palace set-embedder` 的参数，而 supervisor 按指示不动——
少了这个镜像，只写新一节的部署会把一个过期默认值交给那条命令，而那条命令写下的正是
"这个 palace 是用哪个 embedder 建的"，也就是 benchmark 前置检查唯一认的那条记录。

**我们自己的查询路径不再过 `get_embedding_function()`**，直接拿 port。顺手修掉一个真缺陷：
那个函数返回的是 chroma 的 embedding function，`ef([query])` 走的是**文档**侧——于是 E5 的
查询一直是带着 `passage:` 前缀编码的。BGE 两侧前缀都是空的所以没暴露。又一次是同一类失败：
不报错，只是排序变差，和"模型弱"分不出来。

**没有进程级 monkeypatch，也不写 MemPalace 的私有 provider/cache。** Eidolon 的 local
provider 自己读取 `embedding.model_dir`；MemPalace 3.8 的 native provider 没有公开的本地模型
目录接口，因此 native provider 与 `model_dir` 同时配置会在启动前直接报错，而不是导出一个上游
不读取的环境变量或替换 `hf_hub_download`。

两条受支持路径都只经过公开契约：

- 默认 BGE 和 HTTP provider 由 `EmbeddingPort` 生成 document/query vectors，再通过 MemPalace
  3.8 公开的 `embeddings` / `query_embeddings` 写入与查询。
- `minilm` / `embeddinggemma` 仅通过 MemPalace 自己的公开 provider 与
  `get_embedding_function()` 使用；`MemPalaceEmbedder` 只是把这个公开能力包成同一个 port。

整个 `embedding` 一节仍以一个 `EIDOLON_EMBEDDING_CONFIG` JSON 传给子进程，保证模型 identity、
维度和 query/document 前缀来自同一个配置源。创建 collection 时记录的 embedder identity 与读取
时声明不一致就失败，不能回落到另一个模型继续服务。

选型是实测的（`benchmarks/suites/probe_embedders.py`，用真实 run 存下的 39 个 fragment 和
49 个真实查询）：

| 模型 | 维度 | top-1 | top-5 | RSS | ms/次 |
|---|---|---|---|---|---|
| **bge-small-zh** | 512 | 27 → 24 /43 | 36 → 35 /43 | **130 MB** | **1.0** |
| bge-base-zh | 768 | 26 → 24 /43 | 39 → 36 /43 | 130 MB | 3.0 |
| bge-large-zh | 1024 | 24 → 26 /43 | 38 → 36 /43 | 485 MB | 8.6 |
| multilingual-e5-small | 384 | 21 → 24 /43 | 35 → 35 /43 | 180 MB | 1.5 |
| embeddinggemma | 384 | 25/43 | **37/43** | **3 GB** | 44.4 |
| minilm | 384 | 5/43 | 16/43 | 380 MB | 17.5 |

箭头是**同一个模型**在两份语料上的结果——两次完整运行的 palace，分别存了 35 和 36 个
fragment，同样 49 个查询。

**同一个模型在两份语料间摆动 ±3，比三个 BGE 尺寸之间的差距还大。** 语料 A 上 small 的
top-1 最好、large 最差，语料 B 上倒过来。所以 **43 个查询排不出这三个尺寸的高低**，而这份
文档的上一版把那个摆动当成了发现（"base 的 top-1 最好"、"更大并不简单地更好"）——两条都
是噪声。

两次运行**确实**立住的是成本：RSS 和延迟重复到几 MB、零点几毫秒。bge-large 花掉约 3.7 倍
内存、8 倍延迟，换不到可测量的检索收益。所以**默认值是按成本选的**：在分不出高低的模型里
取最便宜的那个。

而 minilm 的 5/43 和 embeddinggemma 的 37/43 **远在那个 ±3 带之外**，所以那两个差异是真的：
minilm 是拿错了工具，embeddinggemma 确实检索最好——它是被 3 GB 排除的，不是被质量排除的。
3 GB 在一台还要跑 eidolon_channel 的 4GB 树莓派上不是"稍稍放宽"，是整台机器。

**Qwen3-Embedding-0.6B 也测了，然后拒了**（它是 decoder 架构、last-token pooling、
指令感知，不是"更大的 BGE"）：两份语料 top5 分别 39/37 和 35/40 —— 单个最高值是它，但
区间 35–40 与其他模型完全重叠，连它自己那个官方 query instruction 的效果都在噪声里
（B 上 +5、A 上 −2）。而成本不在噪声里：**+1088 MB、12–33 ms**，是 bge-small 的 8 倍内存、
12–30 倍延迟。1.1 GB 单凭这一条就排除了树莓派。

为它加的支持**已撤回**（2026-08-05 真的做了）：`qwen3-embedding-0.6b` 从
`LOCAL_EMBEDDING_MODELS` 里删掉，`last` pooling、`position_ids` 的 feed、56 个 KV 张量的
空 cache 一起删掉——没有模型用的 pooling 模式读起来像能力，其实是死代码。

**删之前先量了，不是推的**：把九个模型的 ONNX 声明输入全读了一遍，只有 qwen3 声明
`position_ids` 或任何 past-key-value 输入。所以那三条分支是**不可达**而不只是"没人用"。
顺带量出来的一件事：`token_type_ids` 连按家族分都分不开——bge 三个尺寸和
multilingual-e5-small 声明它，bge-m3、gte-multilingual-base、e5-base/large 不声明——所以
"读图而不是按模型家族分支"这条留着，它是唯一能对的做法。

测量本身保留在 `benchmarks/suites/probe_qwen3_embedding.py` 和
`benchmarks/suites/bench_longmemeval.py`，两个都自带 decoder 的 feed；后者把它列进
`REJECTED_MODELS`，因为"量过然后拒了"和"悄悄跟生产走散了"必须能分开。一个立在没人能重测的
数字上的拒绝，是没人能检查的拒绝。

**记一笔前一版的错**：这段之前写着"已撤回"，但工作区里根本没撤——代码和测试都还在。撤回被
写下来了，没被执行。

要分出三个 BGE 尺寸的高低需要几百个查询而不是 43 个。这是"跑公开 benchmark"的一个具体
理由，与对标 mempalace 无关。

**两个查询五个模型全部 miss**（`我跟客户吵架了`、`我最近工作压力大吗`），四个是全部可部署
候选都 miss（`我以后想做什么`、`我计划去哪里`、`我什么时候开心`、`上周聊了什么`）。这些不是
embedding 缺陷——它们要的是时间范围、commitments ledger、跨 fragment 聚合。换任何模型都不
会好，见下面"召回，不是抽取"。

pooling 和两个前缀是**错了不报错**的那类设置：BGE 用 CLS，E5 用 mean 且两侧前缀是强制的。
第一次测 bge-small 时我漏了这些，相关与无关 fragment 的余弦差是 0.009——看起来像模型没用，
其实是我用错了。所以它们是 `ModelSpec` 的字段并有测试钉住。

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

Agent MCP 只暴露召回所需的 3 个读工具；ops MCP 暴露完整运维面。两者共用同一个
`MemoryService`、router 和 Realm 句柄，不创建第二个 Chroma client。精确工具数由
`test_mcp_surfaces.py` 守护，避免文档数字随运维能力增加而失真。

`search` 与 `recall_context` 回答的是不同问题，故意不走同一条路：search 是"你记得
关于这个的什么"，recall 是"这一轮什么相关"（所以才有图融合、近期轮次、会话过滤）。

---

## 两层可见性

```
audience = "owner"           关于 owner 本人的事实——所有 companion 都可召回
audience = "companion:<id>"  与那一个 companion 之间发生的——对它私有
```

在图的查询里和向量存储的可见性 gate 里强制。来源
（`companion_id`）与可见性（`audience`）是**分开的两个字段**——混在一起会让每条记忆
意外变成私有。

一个 space 是 Owner Realm，同一 Owner 的 Companion 共用它。普通 turn 的 audience 不由
steward 或 prompt 判断，而由经过认证的 runtime context 决定：Companion →
`companion:<id>`，Council → `council:<id>`；两者都缺失时拒绝写入，绝不回退 Owner。
`owner` 是 system/admin materialisation 的显式能力。读侧可见集合为 Owner Shared 加当前
Companion/Council scope，因此共享与私有是同一 Realm 内的访问控制，不是多份 Palace。

这些约束由 `test_scope_policy.py`、`test_companion_audience_isolation.py` 和跨 Companion E2E
共同守护；supervisor 只固定 Realm/Owner，当前请求的 Companion/Council 身份始终随请求传入。

---

## 部署形态

一台机器。向量在 chroma 文件里，图和六个 ledger 在 SQLite 文件里。

**它们不在同一个目录，这是刻意的：**

```
<palaces_root>/<space>/            ← mempalace 的：chroma.sqlite3、mempalace.yaml、它的 marker
<palaces_root>/<space>.ledgers/    ← 我们的：6 个 ledger + knowledge_graph.sqlite3
```

`mempalace repair --mode from-sqlite --archive-existing`（supervisor 换 embedder 时跑的
就是它）会对**整个 palace 目录**做 `os.rename`，然后在原位重建一个新的，最后只拷回一个
文件名——`knowledge_graph.sqlite3` 及其 `-wal`/`-shm`（他们的
`_preserve_knowledge_graph_sqlite`，为其 issue #1816 加的）。

所以我们的东西放在里面时，**每次 repair 都会静默丢掉六个 ledger**，其中两个是产品行为而不
是记账。图活下来只是因为它的名字恰好等于对方硬编码的那个字符串——是和第三方常量的一次巧
合，不是任何契约。

修法是结构性的而不是"记得拷回来"：放到一个 rename 够不着的兄弟目录。既有 palace 在首次打
开时自动迁移（`-wal`/`-shm` 跟着走；用 `rename` 而不是 `os.replace`，同名文件说明目标端
才是活的）。supervisor 一行没动，它原本就在报的 `kg_preserved` 反而变成真的了。

| | |
|---|---|
| 向量 | chroma，palace 内文件 |
| 图 | `<space>.ledgers/` 内 SQLite |
| ledger | `<space>.ledgers/` 内 SQLite，6 个 |
| embedder | 进程内 ONNX 会话，多 palace 共享一份；或 `provider: http` 指向板上的 `eidolon-memory-embedder`（见下），palace 仍在本地 |
| router | `LocalPalaceRouter`——每 space 一把 flock |
| turn ring | 进程内 |
| 进程 : space | **今天 1 : 1**，最后几步落地后 1 : N |

### embedder 是否要单独一个进程

树莓派 5 实测。一个 memory 进程 244.4 MB，其中 **156.4 MB 是 ONNX 会话**；supervisor 每
用户开一个 `agent_runner`（`--memory-space-id` 是单数），所以同一份权重每个用户付一遍。改成
`provider: http` 指向板上一个 `eidolon-memory-embedder` 后，同样的进程 **91 MB**，服务端
216 MB 一次性——**两个用户就回本**，十个用户 2.44 GB → 1.12 GB。

延迟侧的结论和直觉相反。`benchmarks/suites/probe_shared_embedder.py` 在板上跑出的并发一轮
p50 wall：

| 并发 | 每用户私有会话 | 一个共享服务 | 比值 |
|---|---|---|---|
| 1 | 14.1 ms | 16.7 ms | 1.18x |
| 2 | 58.5 ms | 35.7 ms | 0.61x |
| 4 | 116.0 ms | 70.4 ms | 0.61x |
| 8 | 218.5 ms | 136.6 ms | 0.63x |
| 16 | 474.2 ms | 260.7 ms | 0.55x |

绝对值跑一次差 10% 上下，比值不差。回环那一跳只值约 2.6 ms，而且只在**一个**用户时是净亏。
从两个用户起共享服务快约 1.6 倍，负载越高差距越大——四个核上跑 N 个各开 4 线程的会话，输给
一个会话加一条队列。

服务端的并发闸门默认 8，这个数是量出来的，而且**推错了一次**：最初按"会话自己已经把一次
encode 铺到多线程，放进来更多只会把看得见的队列变成看不见的线程争抢"设成 2，实测 8 并发下
闸门 1/2/4/8 分别是 135.7 / 135.8 / 126.4 / 119.7 ms——越大越快。原因是 ONNX Runtime 在
Run 期间放开 GIL，重叠的请求是把 JSON 解析和 HTTP 组帧铺在别人的计算**旁边**而不是后面。
16 并发下 8 之后就平了（233 → 226），再高只多攒住在途请求体，板子省不出这个内存。

3.8 部署使用全新 Palace 和 `bge_small_zh_v15`（512 维）identity；不迁移、不重解释旧向量。
旧 Palace 保留但不挂载，销毁需要独立明确授权。collection identity 不匹配时 Chroma 拒绝打开，
这是守卫在起作用，不是兼容层要绕过的错误。

嵌入式存储有一个硬后果：**一份 palace 只能被一个进程持有**（chroma 没有服务端并发控制，
SQLite ledger 是单写者）。所以第二个持有者被拒绝，这条由测试断言。注意它约束的是
palace 而不是进程——一个进程可以持有很多 palace，这正是 1:N 的空间。

曾经有第二个 router（`SharedStoreRouter`）和 PostgreSQL 的 ledger 与图，用来让同一套
代码服务多主机部署。**已按决定全部删除**（约 2000 行 + milvus 配置管道）。抽象层留下了，
因为它解决的是上面那个问题，不是多形态。

---

## 进度

### 已完成并验证

| | 证据 |
|---|---|
| 脱离 Eidolon OS 独立 | 核心代码不 import 任何 `eidolon_*` 包——由两个守护测试强制（静态 AST 扫描，加一个屏蔽 OS 包后加载所有 entrypoint 的子进程）。contracts 的 46 个测试在只装 pydantic 时通过。**精确说**：核心依赖里唯一的 `eidolon-*` 是 `eidolon-memory-contracts`，那是本仓自己的包（`path = "./contracts"`，仅依赖 pydantic）；`eidolon-data` 只出现在可选的 `eidolon-os` extra 和 dev 里 |
| 契约包自持 | 12 文件 1230 行，仅依赖 pydantic |
| `MemoryReadContract` 已实现 | 8 个方法全部在 `MemoryService` 上；契约**就是**服务本身 |
| 自有图 | schema 与查询在 `kg_sql.py` 一处定义。audience 是列、在 SQL 里过滤；时间戳写入时归一，区间判断是普通 SQL |
| 中文 embedder，公共 API | Eidolon embedding port 生成带 query/document 前缀的 512 维向量；MemPalace 3.8 公共 collection API 接收显式向量。没有私有 provider/cache 注入 |
| **embedder 完全隔离** | 抽象层只剩 `identity()` / `embed_documents` / `embed_queries`；chroma 那套形状退到一个 adapter 里；三个实现（进程内 ONNX、hosted HTTP、mempalace 自己那两个）；换实现只改 `embedding.provider` 一行。**真跑过**：对着一个 OpenAI 兼容端点端到端建出 palace，chroma 持久化了声明的宽度，再用 `provider: local` 去读被 `EmbedderIdentityMismatchError` 挡住并报出两个名字 |
| ledger 行为定义在 Port 上 | 契约测试断言 Port 行为，无一碰 SQLite |
| commitment 状态机在存储之外 | 抽成纯函数，查源码断言副本不会悄悄回来 |
| **local only** | 云端实现全部删除：PG ledger、PG 图、`SharedStoreRouter`、milvus 配置管道、云端 profile、两个 extra |
| ledger 写入有界 | 每 ledger 在 event loop 里串行化，外加一个低于线程池规模的进程级上限 |
| 两层可见性 | 在图查询和向量可见性 gate 里强制 |
| 可观测性 | prometheus `/metrics` 挂在既有端口；contextvar span，字段用 OTel 命名。图的大小/WAL 页数/checkpoint 进度在 checkpoint 循环里采样，不在 scrape 时打库 |
| 遗忘跨全部投影 | assertion/evidence/commitment ledger 先写 tombstone，再删除或归档 drawer/KG；精确 `source_event_id` 可收敛 decision-only、部分投影和完整投影，重放被 tombstone 拒绝 |

当前完整测试与真实进程 E2E 数量、性能结果只记录在 `docs/TEST_REPORT.md`；这里不复制会过期的
计数。发布门要求单元/契约、真实 NATS/Chroma E2E、重启恢复和隐私防复活全部通过。

### 未完成，附真实阻塞

| | 状态 | 阻塞 |
|---|---|---|
| 进程 : space = 1 : N | router、服务、ledger 边界、**以及 MCP 工具面**全都支持了。28 个工具零个再闭包绑定进程句柄：每个都按请求经 router 解析，可选 `memory_space_id` 缺省回落到本进程启动时那个 space，所以既有调用方一行不用改。`agent_runner.py` 仍传 `allowed_spaces=[one]` | 只剩部署拓扑：supervisor 每个 space 起一个进程、discovery 按 space 广播端点。**这一项按你的指示暂缓**，见下一行 |
| NATS 一个 consumer 服务所有 space | 通配 subject 辅助函数已存在；`turn_processor` 本来就从 payload 取 space；工具面的固定句柄阻塞已解除 | `agent_runner` 改订通配 subject 会改变已发布的运行拓扑。本地测试无法证伪它，需要一次真实 Pi E2E 才算数 |
| 单端点 / discovery | | supervisor 掌管进程拓扑——**按你的指示暂缓** |
| 收窄 MCP 响应 | `RecallResult` 按设计不含 `kg_triples` | agent 侧实际是 3 个文件：`infra/memory/port_adapter.py:133-141` 解析、`core/types/memory.py:61` 持有、`domain/context/compiler.py` 6 处消费。比先前记的「`port_adapter.py:201` 一处」和「17 处」都不准，已按实测更正——需要两个仓库同批 |
| 四个公开 benchmark | 口径已对齐、探针已跑两次、超时已按实测调正 | 公开 benchmark 本身仍未跑。**但抽取质量不再是未知数**：2026-09-01 用 `bench_memory_retrieve_quality` 做了一次受控 A/B，见下 |

## 目前最重要的一件事：召回，不是抽取

三次完整灌入（各 40 轮全部处理完），只换 embedder：

| embedder | 正确 | p50 | p95 | RSS | 查询时库里 fragment |
|---|---|---|---|---|---|
| minilm | 11/49 (22.4%) | — | 92ms | 381 MB | 39 |
| embeddinggemma | 23/49 (46.9%) | 575ms | 704ms | 3 GB | 39 |
| **bge-small-zh**（当前） | 21/49 (42.9%) | **26ms** | **30ms** | **133 MB** | 35 |

**这个对比不完全对等，且偏向 embeddinggemma**：它那次库里有 39 个 fragment，bge 那次
35 个——少 4 个可检索文档。steward 是 LLM，逐轮判断在不同运行间不完全一致。写出来而不
抹平：拿输入不同的运行作比较，正是这里四次错误结论的来法。延迟差异不受这个影响。

**抽取不是瓶颈。记忆写进去了。** 当前（bge-small-zh）的按类目分布：

| 类目 | 正确 | 与 embeddinggemma 比 | 读法 |
|---|---|---|---|
| preference | 3/4 | 持平 | 伴侣记忆最核心的用例，可用 |
| canonical_entity | 5/7 | 持平 | |
| emotion / event | 各 2/3 | emotion −1 | |
| time | **3/5** | **+1** | 唯一比 embeddinggemma 好的类目 |
| kinship_alias | 4/8 | 持平 | |
| pronoun | 1/3 | −1 | |
| **topic** | **1/8** | −1 | 最差的类目 |
| **future_plans** | **0/3** | **两个 embedder 都是 0** | 换模型不会好 |
| **abstention** | **0/5** | **两个 embedder 都是 0** | **另一种缺陷，见下** |

**2026-08-04 用同配置重跑一次，测出运行间方差为零**：语料不同（fragment 35→36、
triple 36→33、灌入 1286s→1442s），`correct` 21/49 一模一样，逐查询零翻转。所以
bge 比 embeddinggemma 少的那 2 个答案**不是噪声**，是真差异——上面那条"偏向
embeddinggemma"的免责因此被削弱（它那次 39 个 fragment 比 bge 的 35 多，但实测这个
指标对语料差异不敏感）。

> **2026-09-02 更正：这段的"方差为零"只对 3.8 之前的栈成立，不要再当结论用。**
> 3.8 上同配置三次是 18 / 20 / 19（48 条），也就是 ±2；本节下方"21/49 落在 ±1 带内
> （磁盘记录 21/20/20/20/21/20）"其实早就跟"零翻转"矛盾，只是没人对上。
> 后果是**这套 harness 单跑不能当回归门禁**——两条查询以内的差异它分辨不出来，
> 每个配置至少要重复跑。本节及下方所有 `21/49`、五个 embedder 的对比表，全部是
> 3.8 之前、进程内 embedder、49 条查询的口径，与当前部署不可直接比较。
> 测量与方法见 `docs/TEST_REPORT.md`「What three runs say about the benchmark itself」。

**这三次运行都带着一个渲染缺陷，2026-08-04 才发现并修掉**：`_PREDICATE_ZH` 里 32 个谓词
有 8 个用的是"是…的孩子"这种带省略号的片段，而渲染器只是把 subject + 片段 + object 直接
拼起来，于是"铁锤 是…的孩子 用户"进了 LLM 的 prompt；`self` 这个 schema token 也没被翻译，
以"self 计划 去日本"的形式出现。坏的正是亲属与工作关系——也正是 `kinship_alias` 那个 4/8
的类目。**这是否是它分数低的原因需要重跑才知道**，不能凭形状断言。已改为统一模板，
并有测试断言每个模板都带 `{s}`/`{o}` 两个槽。

**2026-08-04：五个 embedder 跑完端到端，结论是 embedder 不是瓶颈。**

| embedder | 内存 | 端到端 | 召回 p95 |
|---|---|---|---|
| **bge-small-zh** | 130 MB | **21, 21**（零翻转） | **20 ms** |
| bge-base-zh | 250 MB | 20（一次） | 72 ms |
| gte-multilingual-base | 968 MB | 20（一次） | 40 ms |
| multilingual-e5-large | 1541 MB | **20, 21**（翻转 5 个） | 68 ms |
| embeddinggemma | 1532 MB | 23（一次） | 704 ms |

跨 130MB–1.5GB、1.3–59ms、384–1024 维，可部署的四个全部落在 **20–21，无法区分**。
（e5-large 两次运行 20→21、翻转 5 个查询，所以"它比 bge-small 差 1 个"这个说法是错的，
已纠正；embeddinggemma 的 23 只有一次运行，在知道单次会 ±1 摆动后不该当成 +2。）而且**离线探针的 top-1 和 top-5
都不预测端到端，方向还是反的**：top-1 最好的 gte 拿 20，top-5 最好的 e5-large 拿 20，两项
都接近最低的 bge-small 拿 21。所以我用探针筛掉四个候选那一步在方法上是不成立的（探针只对
它直接测量的内存和延迟可靠）。

保持 `bge-small-zh`：可部署的四个里端到端最好，而内存是其余的 1/5–1/12、召回延迟是
1/3–1/35。embeddinggemma 多的那 2 个答案要付 704ms（超 chat 目标 3.5 倍）。

**`future_plans` 与 `abstention` 在两个 embedder 下完全相同** —— 这是离线探针预先做出
的预测，被这次运行验证了。它们不是 embedding 问题：一个要跨 fragment 聚合意图，一个要
的是"没有证据时拒答"这个判断。剩下的类目里证据召回率几乎处处高于正确率，说明召回找到了
所需材料的**一部分**而非全部——是召回内部的完整性与排序问题，不是存储问题。

**abstention 是另一回事**：那 5 个问题在语料里无法回答，正确行为是拒答。零次干净拒答、
零遗漏，意味着它把五个都答了。那是**误导**用户，比答不出更糟，尽管在总分上代价相同。

### 我在这上面连错了四次

前三次探针的准确率测的都是不完整的语料，而三层测量缺陷让每一次的错误推断看起来都比
上一次更有依据：

1. **steward 超时比它等的工作还紧** —— 实测端点最小调用 2.4s、真实 prompt（8.3k 字符）
   22.9s，而限制是 30s（代码默认 20s）。波动就超时，litellm 重试三次 ≈ 93s。现在 90s。
2. **bench 的灌入预算装不下语料** —— turn 严格串行处理，40 轮需约 15 分钟，默认却是
   360s。
3. **排空条件测的是产出而非到达** —— 等 `triples >= 18 且 fragments >= 25`，在第 24 轮
   就饱和，于是 bench 认为完成并查询了缺 40% 语料的库。现在等 turn consumer 积压归零。
4. **以及我从这一切推出的结论** —— 我从"发布 40、7 个 fragment"写下"抽取覆盖率 17.5%，
   是 R@5 的上限"。真实覆盖率是 97.5%。**我三次从聚合数字推结论，都没检查它的输入是否
   完整。**

延迟一直是好的，四次运行 p95 66–110ms，那些查询打的是活服务。


---

## 融合语料：为什么旧语料量不出向量+KG

上面那套 49 题是**向量**基准，拿它评融合会一直得到"无差异"，而原因在语料不在检索。

`companion_corpus.jsonl` 40 轮里只有 11 个实体，`self` 独占 22 轮，**两个非 self 实体
同现的轮只有 6 个**。图的边几乎全部终结于 `self`，于是任意两个事实之间没有可走的路径。
后果是结构性的：

- **提不出多跳问题。** 问"送我鸟的人住哪"需要 鸟 → 人 → 地址两跳，而语料里没有这样的链。
- **提不出失效问题。** `_publish_turn` 给每一轮盖的都是 `datetime.now()`，40 轮落在同一秒
  内。一个事实在第 35 轮被改写，和它改写的那个事实时间戳一样——`valid_from`/`valid_to`
  span 不出区间，渲染层的日/分精度也无从选择。**双时间性存在的唯一理由，基准问不出来。**
- 10 个类目里 6 个在所有运行中 `kg_hits=0`；21/49 落在 ±1 带内（磁盘记录 21/20/20/20/21/20）。
  49 题分辨不出小于 2 题的差异，任何"提升"都不可归因。

`fusion_corpus.jsonl`（50 轮 / 8 个月 / 31 实体 / 36 个非 self 实体对）加两个类目：

| 类目 | 问的是什么 | 只有图能答的原因 |
|---|---|---|
| `multi_hop` | "送我铁锤的人现在住哪个区" | 铁锤和滨江区**从不同现于一轮**，向量单跳答不了 |
| `invalidation` | "我妈现在吃什么药" | 米氮平必须回来，舍曲林必须**不**回来 |

两个类目都会**朝着通过的方向静默失效**，所以断言写在 fixture 上而不是代码上
（`tests/memory/test_fusion_corpus.py`）：多跳两端不得同现、声明的桥接实体必须同时够得着
两端、被取代的事实必须存在且早于取代它的事实、每个 forbidden 词必须是语料真说过的话。
一句对白就能破坏其中任何一条——三次人为破坏各自让对应的断言失败，验证过。

配套改了打分器两处，都是"这个问题根本表达不出来"而非调参：

1. `forbidden_contains` 原先只在 `expect_abstention` 时生效，且正向问题的 `correct` 完全
   忽略 violation。于是召回把新药和旧药一起返回时，两个证据组都命中，记满分——而那句回答
   是"她吃舍曲林和米氮平"。
2. `_publish_turn` 现在认语料自带的 `timestamp`，没有该字段的语料行为不变。

**注意 `abstention` 类目预期仍是 0/6**：打分器要求拒答问题的 `returned_evidence_count == 0`，
而召回总会返回最近邻。这是已知缺陷（旧语料两个 embedder 也都是 0/5），不是本次引入的。
