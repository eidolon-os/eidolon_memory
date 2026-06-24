# Eidolon Memory 多设备架构 Benchmark 中文报告

生成时间：2026-06-24  
最终可信服务链路报告：`reports/memory_perf_20260624_155523/`  
完整多设备矩阵报告：`reports/memory_benchmark_user_full_20260624_150632/`

## 一句话结论

本轮改造后的 memory 架构在本机 benchmark 下通过：

- 多设备记忆隔离正确：其他设备本地记忆没有泄漏到当前设备 prompt。
- 人格/长期记忆可跨设备召回。
- 本机 NATS/JetStream → memory-agent → palace 写入 → MCP 可见链路稳定，最终 run 没有超时。
- 在 5 万条 memory 规模下，召回 p95 仍低于 100ms 级别；服务链路读召回 p95 约 166ms。

## 应该看哪一次结果

管理页 `http://localhost:9001/benchmarks/memory` 里会列出多次 `memory_perf_*`，其中只有下面这一轮是最终修复后的可信结果：

| run | 是否作为结论 | 原因 |
|---|---:|---|
| `memory_perf_20260624_155523` | 是 | 修复 durable name、单 owner 锁、W-01 判定漏洞后重新跑，R-01/W-01 都通过 |
| `memory_perf_20260624_154247` | 可参考 | 已经没有写入超时，但样本较轻 |
| `memory_perf_20260624_154823` | 否 | 暴露 benchmark bug：`timeouts=5` 但旧脚本错误标 PASS |
| `memory_perf_20260624_153457` / `153737` | 否 | 旧 W-01 判定存在漏洞，有 timeout 仍显示 PASS |
| `memory_perf_20260624_13xxxx` / `151648` | 否 | 多数是 read-only 或 NATS 不可用时的探索 run |

## 指标怎么用人话理解

| 指标 | 人话解释 | 这次结果 |
|---|---|---:|
| R-01 | 读/召回一次 memory context 要多久 | 通过 |
| W-01 | 一条 turn 从 NATS 发出，到 memory-agent 写入，再到 MCP 能查到，要多久 | 通过 |
| p50 | 一半请求比这个更快，代表常规体验 | R-01 144ms，W-01 可见 455ms |
| p95 | 95% 请求比这个更快，代表比较稳的上界 | R-01 166ms，W-01 可见 458ms |
| p99 | 极少数慢请求的上界 | R-01 166ms，W-01 可见 459ms |
| timeouts | 写入后等不到可见结果的次数 | 最终 run 为 0 |
| other-device leakage | 其他设备本地记忆错误进入当前设备上下文的次数 | 0 |
| persona hit rate | 人格/长期记忆是否能被召回 | 100% |
| current-device precision | 当前设备本地记忆是否召回准确 | 100% |

## 最终服务链路结果

来源：`reports/memory_perf_20260624_155523/summary.md`

### R-01：读召回

这项模拟 MCP/agent 读取 memory context。

| 项目 | 结果 |
|---|---:|
| 样本数 | 50 |
| 错误数 | 0 |
| 命中率 | 100% |
| p50 | 144.26ms |
| p95 | 165.94ms |
| p99 | 166.06ms |
| SLA | PASS |

解读：读路径稳定，p95 约 166ms，低于当前 SLA 300ms。

### W-01：NATS 写入到可见

这项覆盖完整链路：

`JetStream publish → durable consumer → memory-agent → steward → palace 写入 → MCP 精确查询可见`

| 项目 | 结果 |
|---|---:|
| 样本数 | 5 |
| 超时数 | 0 |
| JetStream publish p95 | 0.69ms |
| 写入到可见 p50 | 455.28ms |
| 写入到可见 p95 | 458.41ms |
| 写入到可见 p99 | 458.41ms |
| SLA | PASS |

解读：NATS publish 本身非常快，不是瓶颈。端到端写入可见主要耗时在 agent 消费、steward 分类、写入和可见性检查。最终没有 timeout，说明 durable subscription 初始化和消费路径已经稳定。

## 多设备矩阵结果

来源：`reports/memory_benchmark_user_full_20260624_150632/matrix_summary.json`

这部分不是服务链路压测，而是验证多设备 memory 架构本身：不同设备数、不同 memory 规模下，召回是否快、是否准确、是否串设备。

### 召回延迟

| 设备数 | memory 数 | 召回 p95 | 召回 p99 | 结论 |
|---:|---:|---:|---:|---|
| 1 | 1,000 | 1.00ms | 2.45ms | 很快 |
| 3 | 1,000 | 1.07ms | 2.20ms | 很快 |
| 10 | 1,000 | 0.80ms | 1.70ms | 很快 |
| 1 | 10,000 | 13.28ms | 22.54ms | 稳定 |
| 3 | 10,000 | 13.72ms | 21.83ms | 稳定 |
| 10 | 10,000 | 11.27ms | 17.79ms | 稳定 |
| 1 | 50,000 | 76.17ms | 158.85ms | 通过 |
| 3 | 50,000 | 83.02ms | 163.93ms | 最慢一组，仍通过 |
| 10 | 50,000 | 74.31ms | 125.60ms | 通过 |

解读：随着 memory 数增加，延迟主要随记录数上升，而不是随设备数明显上升。最差 p95 为 83.02ms，发生在 3 台设备、5 万条记录。

### 同步性能

| 同步事件数 | 每条事件 p95 | 结论 |
|---:|---:|---|
| 100 | 0.435ms | 很快 |
| 1,000 | 0.616ms | 很快 |
| 10,000 | 0.490ms | 很快 |

解读：sync ledger 去重和中心 ingest 的 benchmark 开销很低，远低于目标 `25ms/event`。

### 召回质量与设备隔离

| 指标 | 结果 | 意义 |
|---|---:|---|
| persona hit rate | 100% | 人格/长期记忆可跨设备召回 |
| current-device precision | 100% | 当前设备本地记忆召回准确 |
| other-device leakage | 0 | 其他设备本地记忆没有进当前设备 prompt |
| B01 other-device leakage | 0 | 排序/召回阶段也没有串设备 |

解读：产品体验目标成立：人格记忆共享，设备记忆只强注入当前设备。

### Extension 开销

| 指标 | 结果 |
|---|---:|
| extension overhead 最大值 | -1.22% |
| extension overhead 最小值 | -15.44% |

解读：当前测试没有观察到 extension 带来的正向延迟开销。负值来自测量波动和缓存效果，不代表 extension 会加速，只能说明开销在当前样本里不可见。

## 本轮发现并修复的问题

### 问题 1：JetStream durable name 使用了 dotted memory_space_id

现象：durable subscription 初始化超时。

原因：`default.benchmark.default` 这样的 `memory_space_id` 被直接用于 durable consumer name，而 JetStream API subject 会把 `.` 当成 subject token 分隔符，导致请求路由异常。

修复：新增 NATS-safe name 转换，把 `default.benchmark.default` 转为 `default_benchmark_default`。

### 问题 2：同一个 memory_space_id 被多个 agent 同时持有

现象：benchmark 临时 agent 等不到自己写入的数据，但常驻 agent 日志里能看到这些 turn 被消费。

原因：临时 benchmark agent 和 supervisor 常驻 agent 同时订阅同一个 memory space，违反“一份 palace 一个 owner”的原则。

修复：agent_runner 启动时加进程锁。同一个 `memory_space_id` 如果已经被另一个 agent 持有，会直接失败，而不是悄悄抢 consumer。

### 问题 3：W-01 benchmark 旧判定会误报 PASS

现象：`timeouts=5` 但 `sla=PASS`。

原因：旧逻辑里空样本 percentile 返回 0，导致“没有任何写入可见”反而被算成 0ms 通过。

修复：

- 新增 MCP 工具 `eidolon_memory_get_by_source_turn`，按 `source_turn_id` 精确查写入是否可见。
- W-01 改为只要有 timeout 就 FAIL。
- 不再用随机 token recall 或 5000 条列表扫描判断可见性。

## 当前结论

本机基础组件已经稳定到可以继续跑 benchmark：

- JetStream durable subscription 初始化问题已修复。
- 同 memory space 多 agent 竞争已被进程锁阻断。
- 最终服务链路 benchmark 没有 timeout。
- 多设备 memory 规则在 benchmark 中没有出现跨设备泄漏。

后续如果要把这份报告做进页面，建议 admin benchmark viewer 支持读取 `memory_benchmark_user_full_*` 和 `memory_benchmark_readable_*.md`，现在页面主要展示 `memory_perf_*`，完整多设备矩阵不会自动出现在 UI 里。
