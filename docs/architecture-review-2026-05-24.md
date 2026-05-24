# Architecture review — 2026-05-24

`scripts/` + `reports/` cleanup 后,顺手 review 了 MCP server + NATS worker
两条核心路径,给一份内部审计快照。

---

## MCP server(`entrypoints/mcp_server.py`,388 行)

### 健康

| 项 | 状态 |
|----|------|
| 13 个 tool 全部是 thin shell,业务逻辑下沉 `application/` | ✓ |
| 读 / 写正确分流:读 = 直接 backend,写(`kg_add_triple`/`kg_invalidate`) = NATS publish + 2s sync-feel polling | ✓ |
| Privacy 过滤(`Wing_Privacy`)在 MCP 边界生效(`row_visible_to_listing`) | ✓ |
| FastMCP 装饰器风格清晰,条件注册 KG tools | ✓ |
| 工具元数据(name/description/inputSchema)对外契约稳定 | ✓ |

### 已修复

- ~~`_build_palace_graph` 100+ 行业务嵌在 entrypoints 里~~ → 已搬到 `application/palace_graph.py`,mcp_server.py -99 行

### 未来改进(低优,留作 backlog)

| 项 | 价值 | 风险 |
|----|------|------|
| 工具按主题拆分(recall/kg/palace 三个文件) | 中 | 装饰器集中注册的便利会被破坏 |
| 工具 SemVer / 弃用策略 | 远期 | 现在只有 13 个工具,稳定 |
| 工具级 rate-limit | 远期 | 单用户 chat 流量,Lock 已天然串行化 |

---

## NATS worker(`entrypoints/agent_runner._nats_subscriber_loop`,~95 行)

### 健康

| 项 | 状态 |
|----|------|
| **Pull-subscribe**(不是 push):agent 控流速,LLM steward 慢不会被淹 | ✓ |
| **Durable consumer per user**:`<prefix>-<user_id>`,D1 物理隔离 | ✓ |
| **G1 幂等性**:`turn_processor` + `LockedKnowledgeGraph` 双层防 replay | ✓ |
| **DLQ on max_deliveries**(默认 3):写 `logs/memory_dlq.jsonl` | ✓ |
| **WAL checkpoint 节奏**:每 N 条 turn `wal_checkpoint(PASSIVE)` + `fsync_directory`(D3) | ✓ |
| **同锁**:跟读路径共享 `LockedBackend.lock`,D1 single-owner 不变量在 worker 里也守住 | ✓ |
| **失败分层**(G7):chroma 写失败 → NAK/DLQ,KG 写失败 → ack + log | ✓ |
| **batch fetch 8 条 / 0.5s timeout**:避免空轮询忙等 | ✓ |

### 已知小问题(暂不修)

| 项 | 影响 | 现状评估 |
|----|------|---------|
| **顺序 drain turn → cmd,不交错** | turn 队列热时 cmd 最长等 0.5s | 陪伴单用户 chat-pace 无影响 |
| **固定 batch size 8** | 无法 per-stream tune | 默认值合理,暂不可调用 |
| **`writes_since_checkpoint` 用 nonlocal** | 单 coroutine OK,扩并发 drain 时易出错 | 不打算扩并发 |

### 未来项(backlog,中价值)

| 项 | 价值 | 工作量 |
|----|------|--------|
| **Metrics 出口**:ingest lag、queue depth、steward latency、failure rate(Prometheus / OTel) | 高 | 中 |
| **DLQ replay 入口**:`scripts/replay_dlq.py` 把 jsonl 重发 NATS | 中 | 小 |
| **NATS 显式重连 backoff**:目前依赖 `nats-py` 默认 | 低 | 小 |

---

## 性能基线(post-cleanup,2026-05-24)

| 测试 | P50 | P95 | P99 | max | SLA |
|------|----:|----:|----:|----:|:---:|
| R-01 voice (LiveKit hot path) | 12 ms | **34 ms** | 36 | 38 | ✓ (300ms 富余 ~10×) |
| R-01 non-voice | 388 | 445 | 567 | 569 | n/a |
| R-01 non-voice + KG | 392 | 544 | 664 | 786 | n/a |

Stage breakdown(warm):
- ONNX embed: P50 = 5μs(LRU hit) / P95 = 31ms(LRU miss)
- Lock acquire: P50 = 1μs,P95 = 4μs(D1 锁开销几乎零)
- Vector query (HNSW): P50 = 5.7ms,P95 = 14ms
- Filter + rank: 微秒级

**没有性能回归**。voice path 5-6ms 命中时延 ≈ HNSW + lock,基本到底。

---

## 总判定

**架构是健康的**,本次没有 high-prio 改动。MCP 工具入口清晰,NATS worker
失败模型分层正确,锁开销验证为零。剩 4 个 backlog 项(metrics / DLQ replay /
NATS backoff / 工具拆文件),都是远期演进,不影响当前 SLA。
