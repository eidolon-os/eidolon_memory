# 2026-10-05：合成规模与真实 MCP 召回基线

## 结论

现有 R-01 客户端已对齐当前 actor context 和生产 Agent MCP 入口，错误、降级和预热失败都会使门禁失败。沿用现有 MCP SDK、manifest、percentiles 和生产 ingest 接口，没有引入第二套服务或存储访问层。本轮没有新增生产召回改动。

在本机 Mac、MemPalace 3.10.0、真实 HTTP bge-small-zh provider 上，完成 36 组测试，每组 160 次正式调用，共 5,760 次，另有 144 次预热。全部调用无错误、无降级、无空结果；所有返回的向量记录均通过本次 Realm/受限记录检查。性能随数据量增长，T1 尚未整体通过。

下表是每个规模/模式的 4 组测试（2 批 × 重复/新查询）的 **各组 p95 范围**，不是混合样本百分位，也不是置信区间。

| Facts / themes / KG triples | chat p95，预算 200ms | chat+graph p95，预算 250ms | voice+graph p95，预算 60ms |
|---|---:|---:|---:|
| 1,000 / 8 / 40 | 37.6–42.6ms，4/4 通过 | 41.2–45.6ms，4/4 通过 | 40.7–45.9ms，4/4 通过 |
| 5,000 / 8 / 200 | 111.3–117.5ms，4/4 通过 | 113.8–121.0ms，4/4 通过 | 114.6–117.4ms，0/4 通过 |
| 10,000 / 8 / 400 | 201.0–208.5ms，0/4 通过 | 204.4–210.4ms，4/4 通过 | 205.0–209.7ms，0/4 通过 |

`outcomes.json` 保留失败组的非零退出码。编排脚本继续收集所有组，不把脚本完成视为 SLO 通过；门禁结论读取每组 `sla` 与退出码。

## 测量边界

- 服务为真实 `eidolon-memory-agent` CLI，通过 Streamable HTTP `/mcp` 调用 `eidolon_memory_recall_context`。每组复用一个 MCP session，串行请求，不含 LiveKit 房间、LLM、TTS 或设备出声。
- Chroma 向量 + SQLite KG，嵌入器独立 HTTP 服务、4 线程；模型文件哈希见 `model-hashes.json`。机器、代码版本及 dirty 状态见 `server-*.json`。这是开发 checkout 实测，不能当作发布版本数据。
- 隔离 Realm `mcp-scale-bench`，通过 `LocalPalaceRouter` 和生产批量 `ingest_fragments` 写入；每次 seeding 关闭 router 后才启动 Agent。无 steward 抽取，本实验不能验证写入确认或抽取质量。
- 8 个中文主题模板，事实带唯一编号，逐级增加到 10,000 条。高重复合成语料刻意暴露常见词命中成本，不等于真实家庭语料的分布或质量验收。
- 重复查询为固定 8 问，每组先预热 8 次；新查询为 160 个带独立编号后缀的字符串，不预热。后缀同时改变查询文本，不能将两者差值解释为纯缓存收益。服务按规模重启，冷启动未作为独立 SLO 验收。
- 每逢编号被 37/41/43 整除，分别加入其他 companion、其他 device、do_not_recall 的负例。汇总器检查返回的向量记录，未发现违规；KG 测试数据单独赋予 benchmark audience，此检查不构成 KG 权限负例验收。
- 未测并发负载、真实用户数据、OPi5/NPU、共享推理资源争用或 write ack。

## 已定位的成本与架构判断

以 `5000-chat-repeated-b1` 的正式样本中位数为例：

| 查询 | Chroma 向量查询 | 词法查询 | 向量总计 | 主题召回 | 服务总计 | MCP 总计 |
|---|---:|---:|---:|---:|---:|---:|
| 工作压力 | 23.67ms | 32.38ms | 62.47ms | 42.06ms | 104.81ms | 107.96ms |
| 用户最近的情绪状态 | 23.52ms | 0.51ms | 30.56ms | 11.10ms | 41.96ms | 45.24ms |

每列独立取中位数，不能相加得到总计。`client_minus_service_ms` 包含序列化、调度及未覆盖的服务边界成本，不是纯网络延迟。

安装的 MemPalace 3.10.0 `ChromaCollection._lexical_search_via_sqlite` 在存在 `where` 时取消 FTS 候选 LIMIT，读取全部候选的 metadata/document，再在 Python 过滤及 BM25 排序。这是为了防止错误 scope 占满候选窗口，保证合法词法命中不丢失；不能简单恢复全局上限。源码位置、哈希见 `dependency-evidence.json`（关键位置约 2691、2740、2808 行）。

当前 Eidolon 词法适配路径与主题搜索都调用这个能力。trace 与依赖实现共同支持“高命中词候选读取是重点优化对象”的判断；尚未通过修改后的 A/B 证明具体优化收益。向量查询本身也有规模成本。此前“少于 top_k 就扫 5000 行”的历史描述不能替代当前证据。

下一步应在 **MemPalace 源仓**改进现有词法实现：先复用存储层已有过滤能力，减少不属于 scope/wing 的候选物化；保留 missing/null、逻辑组合、类型比较语义，保持合法候选集合、BM25 统计及稳定排序。若要进一步限定候选或改变评分，需单独评估召回质量。应保护“精确词法命中在向量窗口之外”、错误 scope 大量命中、主题/事实隔离的回归，再复跑相同矩阵。

本工作区未找到 MemPalace 源仓，故没有直接修改安装依赖、复制 SQLite 读取器、关闭词法检索或加入业务缓存。主题仍串行，上一轮并发实验不采纳的结论不变。

## 证据与复查

- `*.json.gz`：完整响应、每次耗时、服务 trace、预热标记、错误、manifest；`summary.json` 为可重建汇总。
- `*.log` / `agent-*.log.gz`：原始客户端和服务日志；`settings.json`：隔离服务配置。
- `compression-index.json`：原文件字节数与 SHA-256。压缩逐一验证解压字节完全一致；没有删除失败或慢样本。
- `measured-run.py.txt`、`measured-client.py.txt`：测量时脚本。测后 runner 仅格式调整；客户端将 manifest `started_at` 移到执行之前。**本次原始客户端 manifest 的 `started_at` 实际为报告生成时刻**，原始证据未改写，逐次耗时不受影响。
- `review.patch`：相对记录的 Git HEAD 的相关代码 diff，包含上一轮已存在的 README 更新；新测试完整保留为 `test_read_benchmark.py.txt`。服务生产改动证据另见上一轮 `recall-concurrency-20261005`。
- `evidence-sha256.json`：归档文件哈希（不含其自身）。

从 `eidolon_memory` 运行无网络汇总：

```sh
uv run --no-sync python benchmarks/results/recall-scale-mcp-20261005/summarize.py
```

复跑服务矩阵时，将 `run.py` 复制到 `benchmarks/results/` 下新的实验目录，修改 `RUN` 为新的临时目录，并确认本机模型路径及 18783/18822/18830 端口空闲；从仓库根执行新脚本。保留原始归档，不能覆盖。需要已安装 `nats-server`、锁定依赖和本地模型，以及本地 TCP 权限。脚本只拒绝复用其临时目录，运行前须自行选择新的输出目录。

验证：本轮基准测试与 scope/契约相关测试合计 **85 通过**；相关 Python 文件 Ruff 通过。上一轮生产取消修复的完整回归为 **1326 通过、11 排除**，本轮只修改基准、测试与文档，没有重跑全量。真实 5,904 次 MCP 调用及压缩后汇总均已完成，临时服务已退出。

最终检查命令：

```sh
.venv/bin/pytest tests/memory/test_read_benchmark.py tests/memory/test_scope_policy.py contracts/tests -q
.venv/bin/ruff check scripts/benchmark/bench_read_livekit.py tests/memory/test_read_benchmark.py benchmarks/results/recall-scale-mcp-20261005/run.py benchmarks/results/recall-scale-mcp-20261005/summarize.py
git diff --check
```
