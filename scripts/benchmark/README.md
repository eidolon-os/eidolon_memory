# Benchmark framework

性能基线与回归测试。所有 bench 都是**离线脚本**(不在 pytest CI 里跑),目的是
在改架构、改默认值、升级依赖后**手动**验证 SLA 是否还能守住,并把结果
归档到 `reports/memory_perf_<timestamp>/`(gitignored)。

## SLA 锁定值

| 指标 | 工具 | SLA |
|------|------|-----|
| **LiveKit recall 端到端 P95** | `bench_read_livekit.py` | ≤ 200 ms |
| **LiveKit recall 端到端 max** | `bench_read_livekit.py` | ≤ 300 ms (硬截止) |
| **JetStream turn → recall 可见 P95** | `bench_write_jetstream.py` | ≤ 5 s(steward LLM 调用主导) |
| **JetStream turn → recall 可见 max** | `bench_write_jetstream.py` | ≤ 15 s |
| **chromadb 单写 P95** | `bench_chroma_write.py` | ≤ 50 ms (FULL sync) |
| **Steward 操作级质量** | `eval_steward_prompt.py` | triples precision ≥ 0.85 / recall ≥ 0.70；invalidations precision ≥ 0.90；should-write ≥ 0.90；privacy errors = 0 |
| **真实召回证据质量** | `bench_memory_retrieve_quality.py` | 报告 full-case accuracy、evidence-group recall、omissions、clean abstention 与 latency |

## 6 个 bench（各自独立）

### R-01 `bench_read_livekit.py` — recall 端到端

```bash
.venv/bin/python scripts/benchmark/bench_read_livekit.py \
    --port 18030 --user-id bench --count 100 --voice
```

通过 MCP HTTP 调 `eidolon_memory_recall_context`,N 次采样,产出 P50/P95/P99/max。
`--voice` 开 LiveKit hot-path 优化(共享 ONNX embedding + skip closets),
`--with-kg` 启用 KG 融合。

输出:`<out>/R-01.json`。

### W-01 `bench_write_jetstream.py` — turn → recall 可见

```bash
.venv/bin/python scripts/benchmark/bench_write_jetstream.py \
    --port 18030 --user-id bench --count 5
```

往 NATS `agent.memory.conversation.turn.<user_id>` 发 N 条 ConversationTurnPayload,
agent_runner 内 steward(LLM)抽 fragment + KG triple,落盘后 publisher 通过
MCP recall 反验"什么时候我能查到刚发的 turn"。

主导延迟是 steward LLM 调用(20-60s 量级),`recall.livekit_timeout_seconds`
对这条链路无意义。

### V10 `bench_chroma_write.py` — 纯 chromadb 写吞吐

```bash
.venv/bin/python scripts/benchmark/bench_chroma_write.py \
    --palace /tmp/bench_palace --count 1000
```

绕开 NATS + steward,直接 `MemPalacePythonBackend.ingest_fragment` 灌 N 条。
用来回归 `chromadb.synchronous` / WAL pragma 变更对持久层延迟的影响。

### J `probe_recall_stages.py` — recall 抖动溯源

```bash
.venv/bin/python scripts/benchmark/probe_recall_stages.py \
    --palace ~/eidolon/memory/mempalaces/bench --count 50
```

把一次 recall 拆成 `ONNX embed → wing fan-out → filter → rank` 4 段,
分别打点。用于诊断"首句 100ms+ 是哪一段在抖"。

### S `eval_steward_prompt.py` — steward 召回质量

```bash
.venv/bin/python scripts/benchmark/eval_steward_prompt.py \
    --dataset tests/memory/eval_steward_dataset.example.jsonl
```

用人工标注的 JSONL 数据集喂给 `LiteLLMSteward.decide()`，把写入链路拆成
extraction、update、should-write、entity resolution 与 privacy 操作分别评分。
报告同时给出 precision/recall、hallucination rate、omission rate 和分类明细。
Steward prompt、模型或抽取契约改了都要跑这个；它只评估候选，不授权写入。

样本必须有 `category`，不得用 `unknown`/`null` 之类占位对象冒充证据。
当前 example 集合覆盖语义角色边界、问题/猜测/不确定表达、敏感信息、更新、
实体消解与 no-write。具体句子是评测数据，不进入生产路由或谓词判断。

### Q `bench_memory_retrieve_quality.py` — 真实证据检索质量

该脚本走隔离 Realm 的真实 `NATS → steward LLM → MemPalace/KG → MCP` 链路。
一个正例只有在全部标注证据组（KG、vector、working memory）分别命中时才算
fully correct；不能再用某一通道的偶然命中掩盖另一通道的遗漏。拒答案例要求
Memory 检索边界不返回无依据证据，Agent 最终是否诚实拒答由 Agent live replay
单独评估。

```bash
.venv/bin/python scripts/benchmark/bench_memory_retrieve_quality.py
```

报告同时保留端到端 MCP latency，避免通过无限扩大 top-k/context 换取表面准确率。
只验证真实管线契约时可用 `--steward-mode test-verbatim --min-triples 0
--min-fragments 5`；质量报告必须保留默认 `llm`，两种结果不得混为同一基线。

## 一键全跑

```bash
# 起 NATS 后:
scripts/benchmark/run_memory_perf_report.sh \
    --user-id bench --port 18030 --read-count 100 --write-count 5 --voice
```

orchestrator 会:
1. spawn 一个 agent_runner subprocess(user `bench`,port 18030)
2. 可选 `--seed S|M|L` 灌 100 / 1000 / 5000 条假 drawer
3. 跑 R-01 + W-01,落盘 JSON + log 到 `reports/memory_perf_<ts>/`
4. 生成 `summary.md`(对照 SLA)
5. 拆 agent_runner

`--skip-pytest` 跳测试;`--skip-write` 跳 W-01(W-01 慢,~10 分钟)。

## 怎么解读 reports/

每次 run 产生一个 `reports/memory_perf_<YYYYMMDD_HHMMSS>/` 目录,gitignored。
里面是:
- `summary.md` — 人读对照表(P95 / 是否过 SLA)
- `R-01.json` / `W-01.json` — 原始数据
- `agent_runner.log` / `seed.log` — 进程日志(失败时溯源)

把 summary.md 截图 / 贴到 PR 里作为性能对比;原始 JSON 用于 diff。

## 历史 baseline

`reports/architecture_d1_baseline_20260519-145231/summary.md` 是 D1 刚落地
的基线对照(R-01 P95 ≈ 30ms / 不同规模线性可控)。保留作历史参照,**不**
作为持续比较基准——后续每次架构改动用新 run 自己对照。

## 实现细节

| 文件 | 角色 |
|------|------|
| `report.py` | `percentiles()` + `sla_pass()` 共享 helper |
| `seed_palace.py` | S/M/L = 100/1000/5000 条 dummy drawer 灌入 |
| `_run_scale_one.sh` | 给 orchestrator 当 inner loop |
| `run_memory_perf_report.sh` | 主入口,管 agent_runner 生命周期 + 产出汇总 |

## 不在范围

- CI 跑 bench — 单次 ~10 分钟,不适合 PR gate
- 跨机网络 latency — 全部 localhost
- LLM 失败重试链 — bench 假设 steward LLM 稳定
- chromadb HNSW 调参 — mempalace 上游负责
