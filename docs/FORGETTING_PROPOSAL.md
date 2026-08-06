# 记忆的演化与遗忘：设计评估

写于 2026-08-06。这份文档要回答的是"这个服务应该怎么忘事"，但它花在**核查前提**上的
篇幅比花在方案上的多，因为核查的结果推翻了提问时的那个前提。

**标注约定**（沿用 `KG_REVIEW.md`）：带 ✅ 的是我自己跑过或逐行读过确认的；文献结论标了
证据等级（论文 / 源码 / 厂商博客）；我在猜的地方写"我在猜"。

**一个必须先说的事实**：这份文档写到一半时，`78717e0`（*the graph's reads all grew with
the graph, and it never shrinks*）落地了，把图的四个读全部重做了一遍。**本文所有图的性能
数字都是在 `78717e0` 之后重跑的**，不是任务书里给的那一组——两组差了一到两个数量级，
见 §2.1。`file:line` 对应 `78717e0`。

那个 commit 自己的结尾把这份文档的题目留了出来：

> Not addressed, and the reason this was worth measuring: **the graph has no forgetting.**
> … Bounded reads move the wall out; they do not remove it. **Under separate evaluation.**

本文的第一个结论是：**那堵墙比它以为的还要远，而且还能再免费推远一次**（§2.3）。

---

## 0. 一句话

三个存储今天有三套遗忘语义，图那一套是"只增不减"——这是真的。但**推动我们去做遗忘的
那个理由（图在语音路径上超预算、几个月后静默失效）今天已经基本不成立**：`78717e0` 那批读
路径改动把四个读里的三个变成了常数时间，剩下一个再加一条覆盖索引还能降 2.1–3.1 倍。✅

所以：**遗忘要按"语义正确"和"字节"来论证，不能按"毫秒"。** 用删用户的记忆去换 30 ms，
而那 30 ms 一条索引就能拿到，是把实现缺陷的代价转嫁给了用户。

真正需要现在做的两件事，都不是"遗忘算法"：一是**"忘掉 X"这条命令今天只删向量、不动图**
（§2.10，我认为这是本文最该先修的一条）；二是**图的大小根本没有指标**，`GRAPH_TIMEOUTS`
涨了没人能对上是为什么（§5.6）。

---

## 1. 现状：三个存储，三套语义

| 存储 | 能忘什么 | 在哪里 | 可逆 |
|---|---|---|---|
| 向量（Chroma，每 space 一个 palace） | `delete_many` 硬删；`archive_many` 打 `privacy=do_not_recall` | `mempalace_python_backend.py:614` / `:637` | 删=否；归档=是 |
| 图（我们自己的 SQLite） | **只有 `invalidate`**，写 `valid_to`。**全仓没有一条 `DELETE FROM kg_*`，没有 prune，没有 VACUUM** ✅ | `kg_sqlite.py:329` | 是（但永不回收） |
| 6 个 ledger | **只有 `command_status` 有 `prune`**，按 `retention_days=30` / `max_records=100k` / 每 100 次写触发一次 | `command_status.py:170`、`:313` | 否 |

`kg_port.py:82-96` 把图这一条写成了设计意图而不是欠账：

> Zero means nothing matching was still valid — which is a legitimate outcome, not a
> failure. **Nothing is deleted**: the row keeps its interval so the history remains
> answerable.

这句话本身是对的——双时态存储就该这样。问题在于它没有下一句：**没有任何东西回答"那这些
行什么时候不再值得留"**。

另外五个 ledger（`extraction_decisions` / `sync_events` / `dlq_entries` / `commitments` /
`canonical_facts`）一条 prune 都没有。`ARCHITECTURE.md:109-116` 那张表已经分好了哪些丢了
会怎样，但那张表回答的是"丢了会怎样"，不是"该不该主动丢"。

### 归档在向量侧不是免费的 ✅

`archive_many` 只改 metadata。被归档的 drawer **仍然在 HNSW 索引里，仍然被向量检索返回**，
然后在 `apply_recall_policy`（`mempalace_python_backend.py:704-723`）里被过滤掉——而过滤
**在 `[:top_k]` 之前**。调用侧传的就是 `n_results=top_k`（`public_recall.py:641`）。

所以一条归档记录如果排进了 top-K，它挤掉的是一条活着的记录，然后自己被丢掉，最后返回的
条数少于 top_k。**归档不是"不再召回"，是"占着名额不再召回"。** 一个用了三年、归档过几百
条的库，每次召回都在为这个付钱。这一条没有测过实际影响，我只核了代码路径。

---

## 2. 先把前提查清楚

### 2.1 图的读路径今天已经不是线性的了 ✅

任务书给的那张表（12 核 Mac）：

```
statements  entities   MB   match p50   query_subjects   query_entity   write
     60025     69903  35.2      79.36           126.98         203.49    7.76
```

我在 `78717e0` 之后用 `benchmarks/suites/probe_kg_scale.py` 重跑了同一条曲线（它自己的
commit message 报的是 28.18 / 0.21 / 1.01 / 0.63 / 0.09，和下面这一行在测量噪声内一致）：

| statements | entities | MB | match p50 | p95 | subjects | combined | entity p50 | write |
|---|---|---|---|---|---|---|---|---|
| 1,000 | 1,335 | 0.8 | 0.37 | 0.42 | 0.12 | 0.93 | 0.59 | 0.18 |
| 5,025 | 6,276 | 3.4 | 1.49 | 1.60 | 0.13 | 0.95 | 0.60 | 0.08 |
| 20,025 | 25,862 | 13.7 | 10.14 | 10.76 | 0.20 | 1.03 | 0.64 | 0.10 |
| **60,025** | **69,903** | **40.7** | **29.20** | 30.15 | **0.23** | **1.06** | **0.65** | **0.10** |

在 60k 这一行上：

| 读 | 任务书 | 现在 | 倍数 |
|---|---|---|---|
| `query_subjects` | 126.98 ms | **0.23 ms** | 552x |
| `query_entity` | 203.49 ms | **0.65 ms** | 313x |
| `add_triple` | 7.76 ms | **0.10 ms** | 78x |
| `match_entities_for_query` | 79.36 ms | **29.20 ms** | 2.7x |

**三个读已经与图的大小无关了**（0.12 → 0.23、0.93 → 1.06、0.59 → 0.65，跨 60 倍数据量）。
做到这一点的是三件事，都在 `78717e0` 里：`ROW_NUMBER() OVER (PARTITION BY ...)` 换成
每个 subject 一条有界分支（`kg_sql.py:196-204` 留了原因）、`query_entity` 的
`subject_id = ? OR object_id = ?` 拆成两条走索引的分支再 UNION、以及一个
`DEFAULT_ENTITY_LIMIT = 200`（`kg_sqlite.py:60`）。

MB 那一列反而从 35.2 涨到 40.7。我没有解释，也不打算编一个——两次测量的 checkpoint 时机
可能不同（WAL 是否已并回主文件），也可能是索引差异。**记下来，不抹平。**

### 2.2 唯一还线性的那个读，线性于实体数，不是陈述数 ✅

`match_entities_for_query`（`kg_sqlite.py:696`）问的是"这段自然语言里出现了哪个已知实体
名"——`instr(query, name) > 0`，通配符在**存储侧**，这个方向索引服务不了，所以它是扫描。

它扫的是 `kg_entities`，不是 `kg_statements`。我在一份 60k 陈述 / 78,758 实体的库上，删掉
21% 的陈述之后重测：

| 状态 | 实体数 | match p50（独立进程） |
|---|---|---|
| 基线 | 78,758 | 46.07 ms |
| 删掉 12,500 条陈述后 | 78,758 | **无变化** |
| 再清掉 14,479 个孤儿实体 | 64,279 | 36.74 ms |
| 上一行再 VACUUM | 64,279 | 35.66 ms |

**删陈述对这个读一毫秒都不省。** 而 `_upsert_entity`（`kg_sqlite.py:319`）是
`INSERT OR IGNORE`，全仓没有任何东西删过实体行——**每一个不同的 object 值都新建一个实体**。
所以实体表是三张表里增长最快、也是唯一影响召回延迟的那张。

顺带一条给探针的更正：`probe_kg_scale.py` 的开头写着实体"大约每八条陈述一个"，但它自己的
输出是 60,025 陈述 / 69,903 实体，**1.16 个实体每条陈述**。`entities_wanted = statements // 8`
只控制了 subject 的长尾，object 是 `f"{_PLACES[n % 6]}{n}"`，每条都不同。这个 fixture 是
实体增长的**最坏情况**，不是它自己声称的 1:8。真实比例是多少——**没有数据**，见 §8。

### 2.3 一条覆盖索引把它再降 2.1–3.1 倍 ✅

`kg_entities` 今天只有主键 `(space_id, entity_id)`（`kg_sql.py:46-55`），没有任何索引覆盖
`name`。所以那个扫描是"走主键索引定位 space，再逐行回表取 name"。加一条
`(space_id, name)` 就变成 index-only scan。用同一个 fixture、同一个真实 adapter 测：

| statements | entities | 现在 | 加索引后 | 倍数 | 建索引耗时 |
|---|---|---|---|---|---|
| 5,000 | 6,668 | 1.60 ms | 0.78 ms | 2.1x | 5 ms |
| 20,000 | 26,044 | 7.65 ms | 2.97 ms | 2.6x | 12 ms |
| 40,000 | 49,585 | 17.72 ms | 5.80 ms | 3.1x | 25 ms |

**倍数随规模变大**（2.1 → 2.6 → 3.1），因为回表的代价随行数增长而 index-only 扫描不。
外推到 60k/69,903：约 29.2 → 8–9 ms。

**这一条是新的。** `78717e0` 把这个读从 79 ms 降到 28 ms 之后，对剩下的部分下的结论是：

> It is still linear, and that is honest rather than fixed. … **A real fix needs a
> trigram FTS table over the names, which is a schema addition worth its own decision.**

那个判断——"通配符在存储侧，索引服务不了"——**对扫描的形状是对的，但它把"能不能免掉回表"
和"能不能免掉扫描"当成了同一个问题**。免不掉扫描，但可以让扫描只走索引页。一条普通的
`CREATE INDEX`，不是 FTS，不改 schema 语义，不引进第二份名字副本，实测拿到 2.1–3.1 倍。
**在考虑 FTS 之前应该先做这个。**

同时测过而**没有**收益的两个改法：去掉 `DISTINCT`、去掉 `ORDER BY length(name) DESC`——
19.30 / 19.22 / 19.21 ms，三者无差别。谓词先把行滤到几乎没有，后面的排序不花钱。所以那两个
子句留着是对的。

### 2.4 于是真正的期限是什么

Pi 5 比这台 Mac 慢 **3–5 倍**（`benchmarks/results/host-profile/README.md`、
`probe_kg_scale.py` 开头都这么写）。语音路径给图 50 ms（`memory_settings.py:52`），超时静默
降级为纯向量（`public_recall.py:541`）。

| 场景 | Mac 上 50 ms 对应 | Pi 上（3–5x） |
|---|---|---|
| 今天（无索引） | ~120k 实体 | **24k–40k 实体** |
| 加覆盖索引 | ~350k 实体 | **70k–120k 实体** |

按探针自己的速率假设（1–3 triples/turn，50–200 turns/天 → 100–500 陈述/天）和实测的
1.16 实体/陈述：

- **今天**：约 **1.5 个月到 1 年**语音路径上的图就不再贡献了。
- **加一条索引**：约 **4 个月到 3 年**。

两个区间都宽得离谱，因为两端的假设（Pi 倍率、每天多少轮、实体比）都是估的。**这不是一个
可以据以设阈值的数字，它只是说明"索引这一步买到的时间，和整个遗忘机制能买到的时间同量级"。**

**结论：在那条索引加上之前，任何以延迟为由的遗忘方案都不成立。** 加完之后，遗忘的理由只
剩两个——字节，和语义。

### 2.5 空间回收：freelist 是错的指标，dbstat 才是 ✅

> **更正**：这一节的第一版结论对，但其中一条测量是错的。我用
> `conn.execute("PRAGMA incremental_vacuum(500)")` 测出它"只回收 1 页"，据此写了
> "incremental_vacuum 对我们没用"。**那是我的 bug**：这个 pragma 返回的是一个需要被
> **步进**的游标，`execute()` 本身只走一步、只释放一页。必须 `.fetchall()` 把它抽干。
> 重测之后结论仍然成立，但理由完全不同——机制是好的，是我们的负载里没有它能搬的东西。
> 下面是重测的数据（SQLite 3.53.2）。

在 46.4 MB / 60k 陈述的库上做一次**散布式**过期（删掉 12,500 条 EVENT 谓词的旧陈述，
21%）——这正是任何按 `valid_from` / 时效性过期的策略会产生的删除形状：

| 阶段 | 文件 | freelist | **dbstat 有效占用** |
|---|---|---|---|
| 满 | 46.37 MB | 0 页（0.0%） | **87.9%** |
| DELETE 后 | 46.37 MB | 45 页（**0.4%**） | **75.5%** |
| VACUUM 后（178 ms） | **37.55 MB** | 0 页 | **92.8%** |

**`freelist_count` 在这个负载上是失明的。** 它报 0.4%，而 VACUUM 收回了 19%。SQLite 只在
**整页变空**时才把页挂上 freelist；散删几乎不产生空页，省下的空间全变成页内碎片。

**`dbstat` 看得见**：`SELECT sum(payload)*100.0/sum(pgsize) FROM dbstat` 从 87.9% 掉到
75.5%，这才是"这个文件里有多少字节是死的"。它是一个虚表，要扫全库，所以是运维/定时指标，
不是热路径指标。

对照组——同样的库，改成**连续**删除（按 `statement_id` 前缀删掉一半），
`auto_vacuum=INCREMENTAL`：

| | freelist | 抽干 `incremental_vacuum` 之后 |
|---|---|---|
| **散布式**过期（我们的形状） | 26 页（0.2%） | 43.91 → **43.81 MB**，1.5 ms，等于没有 |
| **连续**删除（对照） | 3,134 页（**29.2%**） | 43.91 → **31.06 MB**，38 ms，**收回 12.9 MB** |

所以：**`auto_vacuum=INCREMENTAL` 的机制完全正常，只是对按时效性过期这种散布式删除拿不到
东西。** 而它的代价是每个 B 树页多一个 ptrmap 页、并且官方文档明说它会"lead to extra
database file fragmentation"（<https://sqlite.org/lang_vacuum.html>）。**付成本、不拿收益，
所以不开。**

而 VACUUM 对**延迟**几乎无用（§2.2：36.74 → 35.66 ms，约 1 ms）。
**延迟的收益来自行少了，DELETE 就已经拿到；VACUUM 拿到的只有字节。**

### 2.6 要压缩就用 `VACUUM INTO`，不要用 `VACUUM` ✅

WAL 模式下 `VACUUM` 会把**整个新库**先写进 WAL，再 checkpoint 回主文件。同一份数据实测：

| | 耗时 | 结果大小 | 跑完之后残留的 WAL |
|---|---|---|---|
| `VACUUM` | 177 ms | 37.55 MB | **37.77 MB** |
| `VACUUM INTO <新文件>` | **92 ms** | 37.55 MB | **0 MB** |

`VACUUM INTO` 快一倍，写的字节大约是 1/3（重建一次 vs 重建 + WAL + checkpoint 回拷），
不需要对活库拿独占锁，而且**不会改动原库的 rowid**——`VACUUM` 会给没有
`INTEGER PRIMARY KEY` 的表重编 rowid，我们三张 kg 表的主键都是 TEXT 复合键，也就是说
它们**都有隐式 rowid 且都会被重编**。我们自己没有任何地方存裸 rowid，所以今天无害；
写下来是因为这是个"改了不报错"的性质。

代价：`VACUUM INTO` 产出的是一个**新文件**，要换进去就得停写、改名。所以它天然是一个
运维命令，不是后台任务——这正好符合"VACUUM 不换延迟、不该上定时器"的结论。

### 2.7 分块删除：锁持有时间曲线 ✅

`SpaceLock` 是读写锁，写者排他（`space_lock.py:156` — 裸 `async with` 就是写侧）。所以
"压缩持有多久写锁"直接等于"召回最多被挡多久"。同一份数据，删 22,500 行：

| 每批 | 批次 | 单批持锁 p50 | 单批最大 | 总耗时 | WAL 峰值 |
|---|---|---|---|---|---|
| 200 | 63 | **14.0 ms** | 19.5 ms | 706 ms | **7.32 MB** |
| 500 | 25 | 26.6 ms | 35.9 ms | 705 ms | 10.33 MB |
| 1,000 | 13 | 45.1 ms | 55.4 ms | 593 ms | 17.70 MB |
| 2,000 | 7 | 66.1 ms | 79.2 ms | 439 ms | 23.12 MB |
| 5,000 | 3 | 92.5 ms | 110.8 ms | 275 ms | 27.27 MB |
| 一条语句 | 1 | — | ~226 ms | 226 ms | **31.63 MB** |

持锁时间由**每次提交的固定开销**主导，不是行数：200 行 14 ms，5,000 行 92 ms。所以小批次
几乎不增加总耗时（706 vs 275 ms），却把单次持锁降到 1/6，把 WAL 峰值降到 1/4。

Pi 上按 3–5x：**每批 200 行 ≈ 42–70 ms 持锁**。这会让并发的图召回超掉 50 ms 语音预算（设计
上就是降级为纯向量，`public_recall.py:541`），但离 300 ms 的整体截止还很远。**每批 200，
批间释放锁**是我的推荐值。

### 2.8 孤儿实体清理：显然的写法不能用 ✅

想删实体就得先知道谁没被引用。最自然的写法：

```sql
DELETE FROM kg_entities WHERE NOT EXISTS (
    SELECT 1 FROM kg_statements s
    WHERE s.subject_id = e.entity_id OR s.object_id = e.entity_id)
```

**跑了 13 分钟没返回，我杀掉了它。** SQLite 无法用一个 OR 同时命中
`idx_kg_statements_subject` 和 `idx_kg_statements_object`（`kg_sql.py:89`、`:112`）——这是
`78717e0` 里 `query_entity` 刚刚为同一个原因拆成 UNION 的那个坑。

两趟写法：两次 `SELECT DISTINCT` 各走一条索引灌进临时表，再反连接。同一份数据：
**建集合 21 ms + 删 14,479 个实体 68 ms ≈ 90 ms**。

记下来是因为，如果这条清理是别人照着"孤儿实体"这个概念直接写的，它会以一个跑不完的查询的
形态出现在 Pi 上，而不是以一个报错的形态。

### 2.9 一个读事务就能让 WAL 无限涨，而主库停在原地 ✅

这一条和遗忘只有间接关系，但**它是"批量删除"这件事最危险的邻居**，而 `78717e0` 刚落地的
每线程连接（`kg_sqlite.py:151`）把它的暴露面变大了。

WAL 的 checkpoint 只能推进到"任何活着的读者所看到的那个快照"为止
（<https://sqlite.org/wal.html>）。所以一个一直开着的读事务会让 checkpoint 一页都搬不动，
WAL 单调增长，**主库文件停在原地**。在我们自己的 schema 上实测三种读者形态：

| 读者形态 | `wal_checkpoint(PASSIVE)` | 结果 |
|---|---|---|
| A 游标读干净（`list(...)`） | `(0, 865, **865**)` | 正常，`TRUNCATE` 后 WAL = 0 |
| B `for row in cur:` 里 `break`，游标仍被变量引用 | `(0, 865, **0**)` → `(0, 1706, **0**)` | **一页都没搬**，WAL 3.56 → 7.03 MB，`TRUNCATE` 返回 `busy=1` |
| C 显式 `BEGIN` 没提交 | 同 B | 同 B |

**B 正是 `_match_entities_sync`（`kg_sqlite.py:696`）里 alias 那个循环的形状**——
`for row in connection.execute(...)` 里带 `break`。我照着它的字面形状又测了一次：
**匿名游标那一版是安全的**，`(0, 838, 838)`，因为 CPython 在 `break` 之后立刻把游标的引用
计数降到零、语句被重置。

所以：**今天没事，但它的安全性来自 CPython 的引用计数，不来自设计。** 把那个游标绑到一个
变量上（重构时最自然的一步）就变成 B，而 B 的后果是 WAL 无界增长 + 主库冻结——在 SD 卡上
这是最坏的一种失败。而本文提议的批量删除**正好是会让 WAL 猛涨的那种操作**（§2.7 那张表，
一条语句删 22,500 行会产生 31.63 MB 的 WAL）。

配套的两件事：`agent_runner.py:243` 今天做的是 `PASSIVE` checkpoint，它**不报错也不告警**
地返回 `checkpointed=0`；`journal_size_limit` 默认是 −1，也就是不限。压缩落地时这两个都要
处理——至少把 checkpoint 的返回值记下来。

### 2.10 "忘掉 X"今天只删向量，不动图 ✅

这是我在核查里找到的、和延迟无关的一条真缺陷，我认为它比本文任何算法都更该先修。

```
eidolon_memory_forget_preview   → find_forget_candidates（只扫 drawer）
eidolon_memory_forget_confirm   → PrivacyMutationCommand{drawer_ids}   ← 只有 drawer id
   → turn_processor.py:820      → delete_exact_drawers / archive_exact_drawers
                                → backend.delete_many / archive_many    ← 只有向量
```

`PrivacyMutationCommand`（`contracts/eidolon_memory_contracts/kg.py:113`）的载荷里只有
`drawer_ids`；`forget.py:158` 和 `:174` 都只调 `backend`；`turn_processor.py:820-828` 的
分支里没有 `kg`。

后果：用户说"忘掉我妈妈住哪儿"，drawer 没了，三元组还在，下一轮召回时
`transcribe_triple`（`kg_recall.py:63`）把它渲染成 `[KG] 张丽 住在 杭州` 送进 prompt。
**用户会读作"它答应忘了然后没忘"**，而这正是 `ARCHITECTURE.md:136-138` 给
`canonical_facts` 定性为"产品行为而不是记账"时用的同一个理由。

canonical 那条链是**唯一**已经跨两个存储的失效路径
（`canonical_invalidation.py:64-78`：先 `archive_many` 再 `kg.invalidate`），而它只覆盖
canonical fact，不覆盖普通的隐私删除。

**修法是现成的**：两侧都有 `source_turn_id`，而且两侧都有索引——drawer 侧有
`get_by_source_turn_id`，图侧有 `idx_kg_statements_source`（`kg_sql.py:122`）。把它加进
`PrivacyMutationCommand` 就能表达"忘掉这一轮产生的一切"。这也回答了 §5.1。

---

## 3. 别人怎么做

调研用 web search 做的，逐条标了证据等级。**最重要的横向结论：几乎没有系统真的删东西。**
所谓"遗忘"绝大多数是三种之一——排序权重、墓碑、或者"把上一版解释覆盖掉但原文永存"。

| 系统 | 真的自动删吗 | 每次写的 LLM 调用 | 对这块板子 |
|---|---|---|---|
| **MemGPT / Letta** | 否。archival 只进不出 | insert 0 次（但多一轮工具回执）；sleep-time 每 5 轮 (edits+1) 次 | **不行**——SQLite 回退路径没有 ANN，`cosine_distance` 是逐行 Python 回调，N 只增 |
| **Mem0 (OSS)** | **否，v2.0.0 之后连 UPDATE/DELETE 事件都没有了** | 1 次/add，0 次/search | 能跑，但等于引进一个没有遗忘故事的系统 |
| **Zep / Graphiti** | 否，只写 `invalid_at`/`expired_at` | **~4 + 2E 次**（E=抽出的边），且随图变大更贵 | **不行**，还要 Neo4j |
| **A-MEM** | 否。`delete()` 存在但零内部调用者 | 1–2 次 | 能跑，无界增长 |
| **Generative Agents** | 否。衰减纯粹是排序权重 | 1 次/观察 + ~4 次/reflection | 勉强；检索是全量线性扫描 |
| **MemoryBank** | **是**（概率性、不可逆） | **0 次**——遗忘是纯算术 | **可行**，但见下面那个 bug |
| **MemoryOS** | **是**（LFU 淘汰 + 定长 ring） | ~1 次/淘汰 | 可行；注意长期层是硬 `maxlen` |
| **MIRIX** | 否（90% 时压缩，不淘汰） | **1–7 次** | 太贵 |

### 几条值得单独说的

**Mem0 的 DELETE 已经不存在了。**（厂商文档，一手）他们自己的 OSS v2→v3 迁移文档写着：
`add()` 事件"Returns `ADD`, `UPDATE`, `DELETE`" → "Returns `ADD` only"，抽取是
"Single-pass ADD-only (one LLM call, no UPDATE/DELETE)"。
<https://docs.mem0.ai/migration/oss-v2-to-v3>
论文（[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)）描述的那个 LLM 判定
ADD/UPDATE/DELETE/NOOP 的写路径，在今天的 OSS 里是死代码。**引用 Mem0 论文来论证"业界
有 LLM 驱动的遗忘"是引用了一个已经被作者自己删掉的设计。**

**Zep/Graphiti 把遗忘推给了读侧的 LLM。**（源码）失效只是两个时间戳赋值，行、文本、
embedding、出处全留着；更关键的是**失效的边默认连检索都不排除**——缓解手段是把事实渲染成
`"{valid_at} - {invalid_at or 'present'}"` 然后让阅读模型自己偏好 'present'。
这与我们 `transcribe_triple` 里 `（{valid_from} → {valid_to}，已结束）` 的做法**是同一个
设计**（`kg_recall.py:86`），只是我们在 SQL 里就用 `VALID_AT` 滤掉了已结束的，比它严。

**Generative Agents 的衰减因子 0.995 / reflection 阈值 150 都是真的**（论文 + 源码），
但它**不删任何东西**：`AssociativeMemory` 没有 delete/prune/evict，`node_count` 只增。
衰减是一个单调增长的流上的排序权重。**这是本文最该借鉴的一点，也是最容易被误读成"它会
忘"的一点。**

**MemoryBank 是这批里唯一真删的，而它的公式被一个运算符优先级 bug 反转了。** ✅ 我自己拉了
源码确认：

```python
def forgetting_curve(t, S):
    """... The higher the memory strength, the slower the rate of forgetting ..."""
    return math.exp(-t / 5*S)      # 解析为 exp((-t/5)*S)，即 exp(-t*S/5)
```
<https://github.com/zhongwanjun/MemoryBank-SiliconFriend/blob/main/memory_bank/memory_retrieval/forget_memory.py>

紧挨着的 docstring 说"记忆强度越高遗忘越慢"，代码做的是**正好相反**的事：t=7 天时
S=1 保留 0.37，S=7 保留 0.0001。**每一次回想都让这条记忆死得更快。** 而删除是
`if random.random() > retention_probability` 的抛硬币，`pop()` 之后**覆盖写回源 JSON**，
不可逆。

这条不是为了嘲笑谁。它是"遗忘机制的失败是静默的"最干净的例证：这个 bug 不报错、不崩溃，
只是让系统忘掉它最该记住的东西，而任何 recall benchmark 都测不出来。

**形状上最接近这块板子的是 Oblivion**（NEC，[arXiv:2604.00131](https://arxiv.org/abs/2604.00131)，
[代码](https://github.com/nec-research/oblivion)）：`R_t(c) = exp(−n_t(c) / S_t(c))`，
`S_t(c) = (U_t(c) + F_t(c) + ε)·T`，衰减路径里**没有 LLM**，纯算术，而且它明说自己做的是
*"decay-driven reductions in accessibility, not explicit deletion"*——和 §4 的结论一致。

**但我们仍然用不了它**：`n`（距上次访问多少轮）和 `F`（访问频次）都要求记录访问，而那正是
§5.3 因为锁、§4 因为字节两次否掉的东西。**把它记在这里，是因为如果哪天我们真的建了访问
计数，这是该抄的那个公式，不是 MemoryBank 那个。**

### ForgetEval：唯一测过"忘"的 benchmark

[arXiv:2606.15903](https://arxiv.org/abs/2606.15903)，*Control-Plane Placement Shapes
Forgetting*，Dongxu Yang，2026-06。385 例对抗集，五种遗忘原语（supersession / decay /
amnesia / purge / drift）。我自己拉了全文表格确认了下面这几行：

| 系统 | 385 例得分 |
|---|---|
| **MemPalace** | **0 / 385（0.0%）**——论文称之为 "no-deletion-primitive reference point" |
| Graphiti | 8.0% |
| HippoRAG | 8.0% |
| OpenMemory | 48.8% |
| Letta | 52.7% |
| A-MEM | 56.9% |
| LangGraph | 62.9% |
| Mem0 v2.0.2 | 68.3% |
| Lethe+LLM | 91.7% |

它的核心论点和本文一致：*"production failures are predominantly forgetting failures…
yet existing memory benchmarks measure only recall."*

**怎么读 MemPalace 那个 0：** 它测的是**裸 MemPalace**，而我们**没有用它的遗忘语义**——
`delete_many` / `archive_many` 是我们在 `mempalace_python_backend.py:614`/`:637` 自己加的，
带租户校验和写后验证。所以这个 0 不是我们的分数。它说明的是：**我们脚下这个包在遗忘这件
事上什么都没给，凡是有的都是我们自己写的**——包括图那一层什么都没写。

**这条证据的可信度要打折**：单作者预印本，未经同行评审，而且排第一的 Lethe 是作者自己的
系统。我核对了论文里确有此表，**没有**核对它的实现是否公平。

### 我不打算照抄的两个"共识"

1. **"consolidation" 在这批文献里几乎都是"破坏性地覆盖上一版解释"**，不是"合并后丢弃
   原文"。A-MEM 改 tags/context 不碰 content；MemoryOS 用 `merge=False` 整体替换用户画像；
   Letta 的 sleep-time 覆写 block。原始存储照样只增，被销毁的只有**上一次的理解**，而且
   通常没有版本历史。我们的 consolidator（`entrypoints/consolidator.py`）反而更保守——它
   只**新增** `Wing_Theme` drawer，从不覆盖来源。这个方向是对的，不要改。
2. **"LLM 决定忘什么"在这块板子上不成立。** Graphiti 每轮 ~14 次 LLM 调用，MIRIX 1–7 次。
   我们的 steward 一次真实 prompt 就要 22.9 s（`memory_settings.py` 的 timeout 注释），
   而且它跑在总线上不在回复路径里——遗忘如果也要 LLM，就是把这个成本再翻一倍，换一个
   本来用算术就能做的判断。

---

## 4. 人类记忆那套，哪些真能搬

（这一节是委托调研的结果，原文我只抽查了几处；标了链接的可以自己核。）

### 先纠正一个几乎所有人都在传错的东西

**`R = e^(−t/S)` 不是 Ebbinghaus 的公式。** 它出自 Woźniak、Gorzelańczyk & Murakowski
1995——SuperMemo 那批人——而 Ebbinghaus 1885 自己拟合的是**对数**形式：

```
Q(t) = 1.84 / ((log₁₀ t)^1.25 + 1.84)        t 以分钟计
```

用 Murre & Dros 2015 复现里公布的 savings 数据（19 分钟 → 31 天）分别拟合四个模型
（[PLoS ONE 10(7):e0120644](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0120644)）：

| 模型 | R²（Ebbinghaus 1885 数据） |
|---|---|
| `a·exp(−t/S)` | **0.397** |
| **`a·exp(−t/S) + c`（带渐近线）** | **0.850** |
| `a·t^(−b)`（幂律） | 0.975 |
| Ebbinghaus 的对数式 | 0.981 |

**裸指数是四个里最差的，而加一个非零下限就把它救回来大半（+0.45 R²），拟合出的
下限 c ≈ 0.25–0.31。** 这个数字本身就是"该 demote 不该 delete"的全部论证：
**记忆的可取回性衰减到一个正的地板，不衰减到零。**

而且 Ebbinghaus 测的是 *savings*——重学省下多少功夫——不是回忆率。savings 在回忆率已经
归零之后仍然是正的。配上 Bahrick 1984 的 "permastore"（西班牙语保持约 50 年、其中 25 年
基本持平），结论是清楚的：**曲线测的是"够不够得着"，不是"还在不在"。**

### 一张表

| 概念 | 搬得动吗 |
|---|---|
| **Bjork 的 storage strength / retrieval strength**（新失用理论，1992） | **唯一能直接照搬的。** 两个独立的量：存储强度"never lost once accumulated"，取回强度随时间和线索衰减。它正好把"永久保存"和"暂时够不着"分成两件事——而这正是我们需要的语义 |
| 上一条的**不对称更新** | 原文：取回强度**越低**时，一次成功取回带来的存储强度增益**越大**；反过来存储强度越高，取回强度的增益越大。所以"每次访问都 +1"这种规则**方向是反的**——它奖励的恰恰是本来就好找的那些 |
| **渐近线 c** | **搬。** 见上，它比换函数族值钱 |
| **间隔效应 / SM-2 / FSRS** | **形状能搬，常数不能。** FSRS 的 `R = (1 + F·t/S)^(−w₂₀)`、`w₂₀ = 0.1542` 是在约 3.5 亿条 Anki 复习记录上拟合的，而那个任务有一个干净的监督标签（用户按了"忘了"）。**我们一条这样的标签都没有。** w₂₀ 不是关于记忆的事实，是关于 Anki 的事实 |
| **系统性巩固 / CLS**（McClelland et al. 1995） | **分层是真能搬的**（热→批处理→语义层），而且 CLS 解释了为什么交错重放能避免灾难性遗忘。但要诚实：睡眠巩固的效应量近年被本领域内部下修（Cordi & Rasch 2021：*"Current studies failed to replicate large effects"*，而 Rasch 是支持方） |
| **取回诱发遗忘**（RIF，Anderson/Bjork/Bjork 1994） | **不要搬。** 效应小、机制有争议、有过显著的复现失败。而且数据库里根本没有"竞争抑制"这种底物——硬做出来就是凭空发明一条"不常被问的事实更难找"的规则，正是 arXiv:2512.13564 综述点名警告的："heuristic forgetting mechanisms like LRU may eliminate long-tail knowledge, which is seldom accessed but essential" |
| **干扰 vs 衰减** | **没有共识，而这本身有用。** Wixted 2004 引 McGeoch 1932："时间本身不是遗忘的原因，正如时间不是衰老的原因"；Hardt/Nader/Nadel 2013 反过来主张海马里衰减才是主导。**能确定的只有一条：所有人都拒绝"时间本身"当原因。** 落到我们身上就是——不该按"多久没碰"删，该按"有没有被取代"删。而"被取代"我们**已经有精确表示**：`valid_to IS NOT NULL` |

### 一个把设计一分为二的测量

调研里做了一件我没想到要做的事：**同一批数据，指数衰减和 FSRS 幂律，分别用于排序、
混合、阈值三种用途，看结论差多少。**

| 用途 | 换公式的后果 |
|---|---|
| **只用来排序** | **完全没有后果。** Spearman ρ = **1.0000**，top-100 完全重合。两个函数都是 `t/S` 的严格单调减函数，序必然一致 |
| 与相关性加权混合 | 大。w=0.1 时 top-20 只剩 11 个重合 |
| **拿来卡阈值** | **灾难性。** 5,000 条、τ=0.2：指数删 **2,752** 条，FSRS 幂律删 **0** 条。t=8S 时两者差三个数量级 |

**所以：只排序的话，选哪个公式都行，别争了。一旦要卡阈值或混权重，公式的选择就支配了系统
行为，而那个常数必须拿真实数据标定，不能从论文里抄。** 我们没有真实数据（§8），
**这本身就是"第二期只做降级、不做删除"的理由**。

### 访问计数不只是锁的问题，它是字节的问题 ✅

调研测了"每次取回都持久化一次强度"的代价（10 万行库，5,000 次取回）：

| 写法 | 写入字节 |
|---|---|
| 每次取回提交一次 | **63.9 MB WAL（约 12.8 KB / 次读）** |
| 攒进一个事务 | 21.0 MB |
| **读时算，不落盘** | **0** |

一条"读会加强记忆"的规则，会把只读负载变成每次读约 13 KB 的闪存写入。在这块板子上，
**这比 VACUUM 贵得多**。正确做法是**读时用 `(now − last_seen)/S` 现算，只在真实事件发生时
落盘**。这一条和 §5.3 从锁得出的结论是同一个，两条独立的理由指向同一个决定。

### 重要性打分：我们已经有一个，而且已经决定过不信它

`MemoryFragment.importance`（1–5，`fragments.py:51`）由 LLM steward 打
（`steward/rules.py:91`、`steward/llm.py:50`），写进 Chroma metadata
（`mempalace_python_backend.py:491`）——然后**召回时从来没有被读过一次** ✅。它今天只是个
写入闸（`min_importance_to_write=3`）。

而 LLM 打分的可靠性本身是有文献的：*Rating Roulette: Self-Inconsistency in
LLM-As-A-Judge Frameworks*（[EMNLP 2025 Findings](https://aclanthology.org/2025.findings-emnlp.1361.pdf)）
——同样的内容、同样的 prompt、同样的超参，跑两次给不同的分，"often fall short of standard
reliability thresholds"。

**把一个跑两次不一样的浮点数乘进保留策略，等于给"删不删"注入了运行间随机性。** 我们已经
隐含地决定过不在召回里信它；在拿它当删除依据之前，得先解释为什么召回不用它。

### 说得直白些

这一堆里真正能落到代码的只有四条：**(a)** 两列而不是一列（存储强度单调、取回强度衰减）；
**(b)** 衰减到一个正的地板，不到零；**(c)** 该删的是被取代的，不是老的；**(d)** 只排序的
话公式无所谓，卡阈值的话公式决定一切。其余的要么缺 ground truth，要么是给一个我们本来就
想做的决定套一层生物学修辞。

---

## 5. 六个要拍板的

### 5.1 一套模型，还是三套？

**主张：一套策略，三套执行，一个必须原子的接缝。**

不能三套，理由就是 §2.10：向量和图确实持有同一件事的两个形状，而今天删一个不删另一个已经
在生产路径上发生了。

但也不该假装能做成一套。**今天没有 fact 级的跨存储身份**——steward 一轮同时产出
`fragments` 和 `triples`，两者相关但不是 1:1，没有任何字段说"这条 drawer 就是那条三元组"。
唯一真的跨存储的失效链是 canonical（`canonical_invalidation.py`），靠的是
`projection_id`，而它只存在于 `canonical_facts` ledger 里。

**能表达的最小共同单位是 `source_turn_id`**，两侧都有、两侧都有索引。所以：

- **策略**（什么该忘、什么永不忘、什么时候触发）定义一次，在 domain 层。
- **执行**三份，因为三个存储的物理代价完全不同：向量删一条要动 HNSW，图删一条是一行 SQL，
  ledger 大多不该删。
- **原子接缝只有一个**：一条"忘掉这一轮"的命令必须同时落到 drawer 和 triple。它天然属于
  已有的 per-space 写锁临界区——`ARCHITECTURE.md:147` 那条"一个 turn 要原子地写两者"的
  理由，反过来同样成立。

### 5.2 什么永不遗忘

具体清单，不是原则：

| 永不自动删 | 为什么 | 依据 |
|---|---|---|
| `canonical_assertions` / `_invalidations` / `_reactivations` / `_evidence` 全四张表 | `projection_id` 只存在这里；断了链，纠正过的事实重新可召回 | `ARCHITECTURE.md:136-138` |
| `commitments` + `commitment_revisions` | 是 `eidolon_memory_commitments` 的唯一数据源；revision 行同时是幂等键 | `ARCHITECTURE.md:140-141`、`ledger_sql.py:401`/`:427` |
| `dlq_entries`、`sync_events` | 丢了就是丢数据，schema 守卫已经"指名拒绝"重建 | `ARCHITECTURE.md:150-152` |
| user-confirmed 的 drawer（`room` 以 `userconfirm:` 开头 / `metadata.source == "user-confirmed"`） | 是用户明确要求记住的，召回时按策略置顶 | `public_recall.py:34-49` |
| 亲属与出身：`child_of` / `parent_of` / `partner_of` / `sibling_of` / `born_in` | "妈妈叫张丽"没有过期这回事 | `predicates.py` |
| **没有 canonical invalidation 行覆盖的、已失效的三元组** | 见下 | — |

最后一条最不显然，写清楚：**已失效的三元组看起来最该删，但只有当 canonical ledger 里有
对应的失效记录时才安全。** steward 直接产出的 `invalidations`（`turn_processor.py` 的
非 canonical 分支）不写 ledger，图里那一行**就是**"这件事被纠正过"的唯一记录。删掉它，
之后同一个三元组再被说一次，`add_triple` 的第二道幂等闸（"同 (s,p,o) 且
`valid_to IS NULL`"，`kg_sqlite.py` 的 `_add_triple_sync`）不会命中任何东西，于是它作为
新事实重新生效。**遗忘一条纠正，等于撤销那条纠正。**

`sensitive = 1` 的三元组（`has_health_condition` / `takes_medication` / `has_symptom`）
**不进自动策略**。不是因为它们该永存，而是因为"自动删健康记录"和"用户要求删健康记录"是
两件不同的事，后者已经有路径了。默认不召回意味着它们不占召回质量，只占字节。

### 5.3 触发器

四个候选，逐个查了它们在今天的代码里存不存在：

| 候选 | 现状 | 结论 |
|---|---|---|
| **访问时间 / 频次** | **完全不存在**。没有 `last_accessed`、`access_count`，drawer 和三元组都没有 ✅ | **不做，两条独立的理由。** ①**锁**：读走的是 `SpaceLock` 的读侧（`kg_sqlite.py` 全部 8 个读），读里写就是把读者变成写者、排他掉所有并发读——正是 `space_lock.py:20-26` 刚花力气修掉的那件事。②**字节**：实测每次读约 **12.8 KB** 的 WAL 写入（§4），在这块板子上比 VACUUM 贵得多 |
| **重要性** | 存在但从不被召回读（§4） | **不做**，至少在召回开始用它之前不做 |
| **年龄** | `valid_from` / `recorded_at` 都在。`recorded_at` 存了但从不出现在任何 WHERE 里（`KG_REVIEW.md` §3） | **做，但只作为选择条件，不作为触发条件** |
| **大小 / 延迟预算** | 可测：`stats()` 已有（`kg_sqlite.py:825`），`probe_kg_scale.py` 已有 | **做。这是唯一有可观测危害支撑的信号** |

**推荐：按大小触发，按"时效性 + 是否被取代"选择，永远不按访问。**

选择条件的形状（不是最终参数，参数需要 §8 里那个没有的数据）：

```
候选 = 三元组 且 predicate 的 temporality == EVENT
     且 valid_from 早于 N（比如两年）
     且 不在 §5.2 的白名单里
或者 = 三元组 且 valid_to IS NOT NULL
     且 存在覆盖它的 canonical invalidation 行
     且 valid_to 早于 N
```

**但 `PredicateTemporality` 今天还不能用来做这件事** ✅。它是对的抽象——`KG_REVIEW.md`
§12 决定删掉 `kg_window_days` 时给的理由就是"`PredicateTemporality` 已经按谓词分好时效性，
那才是对的形状"——但它的**内容没填**：

```
32 个谓词：durable 24 / current_state 5 / event 3
event        = attended, experienced, achieved
current_state = lives_in, has_state, has_emotion, takes_medication, has_symptom
```

24 个 durable 里躺着 `has_concern`、`worried_about`、`struggles_with`、`planned_to`、
`does`、`practices`、`uses`——这些显然不是 durable，它们只是**继承了
`_definition()` 的默认值**（`predicates.py:45-59`）。真正被人分过类的只有 9 个，其余 23 个
是默认值。

**所以按 EVENT 做衰减，今天只会命中 3 个谓词，几乎什么都删不掉。填完这张表是遗忘机制的
前置条件，而且它是纯产品判断、不需要写任何存储代码。** 这是第一期该做的事。

### 5.4 在哪里跑

约束：Pi 没有空核；压缩阻塞一个 turn 或一次召回，比图大要糟；服务除了 consolidator 之外
没有自己的调度器。

四个位置，我核了每一个：

| 位置 | 评估 |
|---|---|
| turn 路径内联 | 否，直接阻塞对话 |
| 新起一个调度器 | 否，凭空多一个进程和一份 supervisor 配置 |
| consolidator 进程 | 它已经被 supervise、已经按 `--interval-hours` 循环、而且**只通过 NATS 摸这个 space**（`consolidator.py:1-31`、`agent_runner.py:262`），所以不会变成第二个文件持有者。但它的读通道只有 `list_drawers`，图根本读不到——要用它得新开一个查询 subject 和一条命令，**为一个不需要 LLM 的判断引进一整条 NATS 往返** |
| **`agent_runner` 里已有的 `_checkpoint_forever`** | 它**已经是**一个周期性维护任务，**已经**为了 checkpoint 图的 WAL 而拿整把 space 写锁（`agent_runner.py:215-260`，第 246-249 行 `async with lock:`），**已经**在数写入次数 | ✅ |

**推荐：把 `_checkpoint_forever` 扩成一个维护任务，而不是新造调度。** 理由是它已经具备了
需要的全部三件事——周期、写锁、写入计数器——而且它的失败模型已经写好了：
"a checkpoint failure degrades gracefully (log + retry) instead of forcing a NATS
reconnect"（`agent_runner.py:218-221`）。压缩失败应该完全一样。

触发条件用**空闲**，而不是时钟：那个循环里的 `writes_since_checkpoint` 已经知道最近有没有
在处理 turn。"连续 N 分钟没有 turn 才开始压缩，一有 turn 到就停在当前批次边界"——不需要
时钟、不需要配置时区、也不需要假设用户几点睡觉。

参数：每批 **200 行**（§2.7：Mac 14 ms / Pi 约 42–70 ms 持锁），每次 pass 有总上限，
批间 `await` 让出。

### 5.5 可逆性

| 方案 | 评价 |
|---|---|
| 硬删 | 不行。这是伴侣记忆，一次错误的策略调整会不可逆地吃掉几年的东西——MemoryBank 那个抛硬币+覆盖写回就是这个失败的完成态 |
| 墓碑（加一列 `forgotten_at`） | **在图这里买不到东西**。我们要去掉的成本就是"表里的行"（§2.2 那个扫描），一条打了墓碑的行还是一行。它在**向量侧**倒是已经存在，就叫 `archive_many`——而 §1 说了它还占 top_k 名额 |
| **先导出再删除** | ✅ 推荐 |

具体：把命中的行按 JSONL 写到 `<space>.ledgers/forgotten/<date>.jsonl`，**fsync，验证可读，
再** DELETE。字节代价只有原文，没有索引；运维用一个脚本就能读回来。

### 时序数据库那边早就吵完这一架了

我们不是第一个面对"双时态存储要不要物理删"的。三个可引的先例，结论高度一致：

**SQL:2011** 直接把它写进标准（[Kulkarni & Michels, SIGMOD Record 41(3)](https://sigmodrecord.org/publications/sigmodRecord/1209/pdfs/07.industry.kulkarni.pdf)）：

> **UPDATE and DELETE on system-versioned tables only operate on current system rows.
> Users are not allowed to update or delete historical system rows.**

而它**完全没有定义任何清理机制**——所有 purge/retention 都是厂商扩展。

**Datomic** 的 excision 文档是这件事写得最清楚的一份
（<https://docs.datomic.com/operation/excision.html>）：

> **Legitimate motivations for removing data are very rare.** … Excision is designed to
> support the following two scenarios: **Removing data for privacy reasons** [and]
> **Removing data older than some domain-defined retention period**.
> … **Excision should never be used to correct erroneous data.**

以及一条我认为应该直接照抄的性质：

> the excise attributes themselves are protected from excision, so **there is no way to
> 'erase your tracks.' Every excision creates a permanent record.**

**Snodgrass**（*Developing Time-Oriented Database Applications in SQL*，
[免费 PDF](https://www2.cs.arizona.edu/~rts/tdbbook.pdf)）§9.5 把它叫 vacuuming，
并且明说：

> **Vacuuming is a dangerous operation because it violates the underlying semantics of
> the transaction-time state table.** … The danger here is that a projection might have
> been erroneously deleted only yesterday, yet we have just vacuumed away all evidence.

他提了两条约束，我认为都该照搬：**必须有 vacuum log**（他给了 DDL），以及**谓词必须单调**
——"once it is satisfied, it will continue to be satisfied"。一条"删掉 1–2 年前的"规则
不单调：它会删掉本来在更晚的时刻会被判为该留的东西。**我们的候选条件（§5.3）用的是
`valid_from < N` 这种形式，是单调的；任何带上界的区间条件都不是。**

还有一条 §10.6 的经验判断，可能直接省掉我们一整期工作：

> often the history store is the largest component of a bitemporal state table, implying
> that **vacuuming the archival store may not be effective in substantially reducing the
> size of such a table.**

翻成我们的话：**双时态库里占地方的通常是有效时间的历史，不是"纠正"的历史。** 我们的图正是
这个形状——`triples_invalidated` 相对 `triples_total` 大概率很小（没测过，见 §8）。所以
"删掉失效的三元组"这条路，很可能收不回多少字节。

### 四条从现有代码和上面这些抄来的规矩

1. **写后验证**。`delete_many` 删完会再 `get` 一次确认不可见，不可见才返回
   （`mempalace_python_backend.py:625-629`）。压缩必须一样——删完 count 对不上就中止整个
   pass 并告警，不要继续下一批。
2. **宁可失败也不要静默截断**。`find_forget_candidates` 命中上限时抛
   `ForgetResolutionLimitExceeded` 而不是删一部分（`forget.py:140-152`）。
3. **导出目录的名字不能撞上任何清理 glob**。`history_reset.clear_repair_archives` 会
   `shutil.rmtree` 掉 `*.pre-rebuild-*`——`KG_REVIEW.md` §10 P0-2 记的就是这个形状的事故。
   `forgotten/` 不匹配现有任何一条，但这一条要写进注释里，不然下一个加清理规则的人不会知道。
4. **遗忘记录本身不可被遗忘**（Datomic 那条）。`forgotten/` 下的文件不进任何保留策略，
   也不进任何清理 glob。**"忘了什么"必须比"被忘掉的东西"活得久。**

### 5.6 怎么度量

**先补一个今天没有的东西：图的大小根本不是指标。** `stats()` 存在
（`kg_sqlite.py:825`，返回 entities / triples_total / triples_active / triples_invalidated），
但 `support/metrics.py` 里没有对应的 Gauge。而 `GRAPH_TIMEOUTS`（`metrics.py:90`）的
docstring 自己写着"a rate that climbs is the signal"——**可是没有任何一条时间序列能让人把
那个上升对上图的大小**。这是"图静默停止贡献、没有告警"的机制性原因，而且它和遗忘无关，
是先决条件。

四个数字，各证明一件不同的事：

| 数字 | 证明什么 | 从哪来 |
|---|---|---|
| `probe_kg_scale.py` 的 `match_p95`，**在板子上跑** | 图还在语音预算里 | 已有；`probe_host.py` 已经会打印 `fits` / `BLOWS` |
| 新增 Gauge：`graph_statements` / `graph_entities` | 增长曲线本身——尤其是**实体数**，那才是 `match` 的自变量（§2.2） | `stats()` 已有 |
| **`dbstat` 有效占用率**，不是 `freelist_count` | 文件里有多少字节是死的。§2.5 实测 freelist 报 0.4% 而真实死字节是 12 个百分点 | `SELECT sum(payload)*100.0/sum(pgsize) FROM dbstat`；虚表，扫全库，只能定时跑 |
| `wal_checkpoint()` 返回的 `checkpointed_pages` | WAL 有没有真的被并回去。§2.9：一个读者就能让它长期为 0，而今天**没有任何地方记录这个返回值** | `agent_runner.py:243` 已经在调，只是丢掉了返回值 |
| 49 题 dev 套件的 `correct`，压缩前后 | **不倒退**。`ARCHITECTURE.md:492-495` 实测过它运行间方差为零、可当回归门禁 | 已有 |
| 导出文件行数 == DELETE 的 rowcount | 可逆性真的成立 | 需要写 |

**必须诚实的一点**：第三条只能证明**无害**，证明不了**有益**。那套语料 40 轮、35 个
fragment，而策略针对的是几年；它不可能显示"忘掉三年前的旧事让召回变好了"。
**"遗忘提升了召回质量"这个命题，我们今天没有任何手段可以验证**，所以方案里不要写这个目标。
唯一能立住的目标是：**在不损失可测质量的前提下，把增长压回可控。**

---

## 6. 建议：四期

**第一期——不删任何东西**（先决条件，也是本文认为唯一现在就该做的一批）

1. 加 `kg_entities (space_id, name)` 覆盖索引。§2.3 实测 2.1–3.1x，随规模变大，25 ms 建成。
2. 把图的大小接进 `/metrics`。没有它，后面每一步都是盲的。
3. 补 `PredicateTemporality`：把 23 个继承默认值的谓词逐个分类。纯产品判断，不动存储。
4. 修 §2.10：`PrivacyMutationCommand` 带上 `source_turn_id`，"忘掉 X"同时失效图里的三元组。
   **这是本文唯一一条我认为不该等的。**

做完第一期之后重跑 `probe_kg_scale.py`（在板子上），再决定还需不需要第二期。**很有可能
不需要，或者不需要那么快。**

**第二期——降级，不删除**

`invalidate` 之外加一个取回层的概念（Bjork 的 storage/retrieval 之分）：命中衰减条件的
三元组从召回读里排除，行留着，可逆。图侧的实现是 `VALID_AT` 旁边多一个条件；向量侧
`archive_many` 已经是这个语义了。

**判据必须是单调谓词，不能是标定出来的分数阈值**——`temporality == EVENT AND valid_from <
N`，而不是 `decay_score < τ`。两个理由：Snodgrass 的单调性要求（§5.5），以及 §4 那个实测
——阈值这一步会让公式的选择支配一切，而我们没有可以标定它的数据。

这一期能证伪一件重要的事：**如果降级之后 49 题不变、而用户也没抱怨，说明这些记忆本来
就没在起作用，删掉是安全的。如果有东西变差了，我们在删之前就知道了。**

**第三期——先导出，再删除**

只在第二期的降级集合上做，且只对 §5.3 的候选条件。跑在 `_checkpoint_forever` 扩出来的
维护任务里，空闲触发，每批 200 行，**批间做一次 `wal_checkpoint` 并记录它的返回值**
（§2.7 的 WAL 峰值 + §2.9 的读者饿死，两个问题一个动作），写后验证。

同时做孤儿实体清理（两趟写法，§2.8）——**这才是真正让 `match` 变快的那一步，删陈述不是。**

**第四期——字节，也许永远不做**

用 **`VACUUM INTO` + 换文件**，不用 `VACUUM`（§2.6：快一倍、写 1/3 的字节、不动原库 rowid、
不需要对活库拿独占锁）。做成一个运维命令，触发条件是 `dbstat` 有效占用率掉到某条线以下，
由人来跑，不上定时任务。

**而且它可能根本回收不了多少。** Snodgrass §10.6 的经验是双时态表里占地方的是有效时间的
历史而不是纠正的历史（§5.5）——我们的 `triples_invalidated / triples_total` 比例没测过，
如果它很小，这一期就没有意义。**先看指标再决定要不要做，这是把它排到最后的原因。**

---

## 7. 我不会做的，以及为什么

| 不做 | 为什么 |
|---|---|
| **带阈值的连续衰减分数** | 阈值那一步要求标定一个常数，而实测表明公式选择在**阈值**用途下支配一切（指数删 2,752 条、幂律删 0 条，§4）。我们没有可以标定它的数据。**只排序则无所谓，所以第二期只排序、不卡阈值** |
| **读侧写入的"取回加强"** | 两条独立理由：锁（读者变写者）和字节（每次读约 12.8 KB WAL，§4）。MemoryBank 有这个信号还是把公式写反了，而且没人发现 |
| **LLM 判断该忘什么** | Graphiti 每轮 ~14 次调用。我们的 steward 一次 22.9 s，而它跑在总线上不在回复路径里；遗忘也走 LLM 就是把这个再翻一倍，去换一个算术能做的判断 |
| **`PRAGMA auto_vacuum=INCREMENTAL`** | 机制没问题（连续删除的对照组回收了 12.9 MB），但我们的过期是散布式的，freelist 只有 0.2%——**抽干了也只回收 0.10 MB**（§2.5）。付 ptrmap 页和额外碎片的成本，拿不到东西 |
| **定时 VACUUM** | 不换延迟（§2.5）。而**不是**因为 SD 卡磨损——那个担心经不起算账：按最坏的 3× 写放大、1 GB 库每月一次，五年 180 GB，占工业卡顺序写 TBW 预算的 **0.04%–1.1%**，比日常小随机写低两三个数量级。理由是它没有收益，不是它有代价 |
| **拿 `freelist_count` 当膨胀指标** | 实测它报 0.4% 而真实死字节 12 个百分点（§2.5）。任何基于它的自动触发都会永远判断"不需要压缩" |
| **LRU / 最少访问淘汰** | 除了没有访问数据之外，arXiv:2512.13564 点名它会吃掉"seldom accessed but essential"的长尾——而伴侣记忆里最珍贵的那些恰好就是长尾 |
| **删掉未被 canonical 覆盖的失效三元组** | 那一行就是"这件事被纠正过"的唯一记录，删了等于撤销纠正（§5.2） |
| **给 ledger 加通用 prune** | 五个里有四个丢了就是丢数据或丢产品行为。`command_status` 有 prune 是因为它明确是投影、可重建（`command_status.py:44-49`），这个性质其他四个没有 |
| **换图数据库 / 换向量库** | 任务书排除了，而且 §2.1 之后也没有理由：三个读已经是常数时间，剩下那个是扫描而不是查询，换存储不改变它的方向 |

---

## 8. 我在猜的地方

1. **真实的实体:陈述比是多少——不知道。** 探针的 fixture 给 1.16（而它自己的文档说 1:8，
   §2.2），这是最坏情况。真实值决定了 `match` 的增长速度，也就决定了整个方案的紧迫性。
   本机四个 palace 的图**全是空的**（`kg_statements` 计数为 0）✅，benchmark 语料只有
   33–36 个三元组。**这是本文最重要的缺口**：一份跑够几个月的真实图，会比这里任何一条
   推理都有用。
2. **Pi 的 3–5x 是继承来的，不是我测的。** §2.4 那两个区间的宽度基本全来自这个假设。
   `probe_kg_scale.py` 在板子上跑一次就能把它换成实测。
3. **每天 100–500 条陈述**是探针文档里的假设，来源是 e2e 套件的 1–3 triples/turn 加一个
   "engaged user 50–200 turns/天"的估计。没有真实使用数据。
4. **归档 drawer 占 top_k 名额的实际影响没有测过**（§1）。我核了代码路径，没有量过它在一个
   归档比例高的库上损失多少召回。
5. **ForgetEval 那张表我核了论文原文，没核它的实现。** 单作者预印本，未同行评审，榜首系统
   是作者自己的。MemPalace 的 0/385 测的是裸包，不是我们的封装。
6. **§4 的人类记忆文献是委托调研的结果，我只抽查了几处**（Ebbinghaus 的对数式与
   Woźniak 归属、Bjork 的原话、FSRS 常数）。自己拉过源码的只有 MemoryBank 那个优先级 bug。
   §4 那三组拟合与仿真（R²、ρ=1.0000、阈值 2752 vs 0、12.8 KB/次读）**是调研跑的，不是我跑的**。
7. **`triples_invalidated / triples_total` 的真实比例没测过。** 它决定第四期有没有意义
   （§5.5 引的 Snodgrass §10.6），而它需要的还是那份不存在的真实图。
8. **§2.1 那个 MB 从 35.2 涨到 40.7 我没有解释。**
9. **VACUUM 在真实 SD 卡上的吞吐没有实测**，§7 那个 0.04%–1.1% 是从 NVMe 数字外推的，
   而且用的是**工业卡**公布的 TBW。bunnie & xobs 在 30C3 说过消费级卡里"anything from
   high-grade factory-new silicon to material with over 80% bad sectors"——**在消费级卡上
   任何耐久度计算都不成立，包括这一个。**
10. **第二期"降级"能不能被 49 题证伪，我不确定。** 那套语料 40 轮，很可能对降级完全不敏感，
   于是第二期什么都证明不了——那就得承认这一步是靠推理而不是靠测量走的。
