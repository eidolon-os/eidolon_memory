# Benchmark framework

性能基线与回归测试。所有 bench 都是**离线脚本**(不在 pytest CI 里跑),目的是
在改架构、改默认值、升级依赖后**手动**验证 SLA 是否还能守住,并把结果
归档到 `reports/memory_perf_<timestamp>/`(gitignored)。

## SLA 锁定值

| 指标 | 工具 | SLA |
|------|------|-----|
| **MCP recall P95（当前优化目标）** | `bench_read_livekit.py` | voice ≤60ms / chat ≤200ms / chat+KG ≤250ms |
| **LiveKit recall 端到端 max** | `bench_read_livekit.py` | ≤ 300 ms (硬截止) |
| **JetStream turn → recall 可见 P95** | `bench_write_jetstream.py` | ≤ 5 s(steward LLM 调用主导) |
| **JetStream turn → recall 可见 max** | `bench_write_jetstream.py` | ≤ 15 s |
| **chromadb 单写 P95** | `bench_chroma_write.py` | ≤ 50 ms (FULL sync) |
| **Steward 操作级质量** | `eval_steward_prompt.py` | triples precision ≥ 0.85 / recall ≥ 0.70；invalidations precision ≥ 0.90；should-write ≥ 0.90；privacy errors = 0 |
| **真实召回证据质量** | `bench_memory_retrieve_quality.py` | 报告 full-case accuracy、evidence-group recall、omissions、clean abstention 与 latency |

## 7 个 bench（各自独立）

### R-01 `bench_read_livekit.py` — MCP recall 端到端

```bash
uv run --no-sync python scripts/benchmark/bench_read_livekit.py \
    --url http://127.0.0.1:18030/mcp \
    --memory-realm-id bench --owner-id benchmark --companion-id benchmark \
    --count 160 --warmup 8 --voice --with-kg \
    --server-manifest /path/to/server-manifest.json \
    --out benchmarks/results/<run>/voice.json
```

使用当前actor契约与Agent `/mcp` surface；旧tenant/persona/instance参数已失效，不再接受。
复用一个MCP session，按固定查询顺序测量；`--query`可重复指定。调用方负责服务启停和隔离。

原始样本包含预热、失败时延、records、KG、server trace、降级原因。MCP `isError`、畸形响应、
超时、降级、会话建立失败均不能PASS；任何预热失败同样失败。合法空结果单独计数，不能当作
检索质量命中。总耗时与成功调用耗时分别统计，不丢失败样本来降低p95。

门槛使用上表P95目标；旧脚本内部300ms门槛和未实际执行的p99字段已移除。20次以下或小数据
即使显示PASS也不能当完整验收。服务器provenance由启动方提供，客户端版本不能冒充服务器版本。

输出格式为 `manifest / summary / warmup / per_call / sla`，`--out`必填；输出含召回内容，
仅对合成数据或获准归档的脱敏语料使用。长期结果写入可提交目录，真实个人数据不能直接入库。

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

### J `probe_recall_stages.py` — production recall 分段诊断

```bash
EIDOLON_MEMORY_SETTINGS_YAML=/path/to/isolated-settings.yaml \
uv run --no-sync python scripts/benchmark/probe_recall_stages.py \
    --user-id bench --chat --with-kg --count 160 --warmup 8 \
    --out benchmarks/results/<run>/chat-graph.json
```

通过现有 LocalPalaceRouter 调用真实 `recall_with_kg_fusion`，保存其trace、原始结果ID、
降级状态和manifest。省略`--chat`测voice，省略`--with-kg`关闭图谱。
`--clear-cache`在每次调用前清query embedding缓存，测冷查询而非冷启动。

使用隔离配置/Realm；router必须取得该空间的独占持有权。真实bge模型通过已有embedding
HTTP服务配置，不能用测试向量宣称provider性能。预热原始样本单独保留，任何vector降级
（包括预热）使进程退出非零。分段可重叠，不能相加得到总延迟。

这不是MCP/Agent端到端或质量基准。旧版探针手工重建部分搜索，旧输出不与新版直接比较；
`--cold-rounds`改为`--warmup`，输出结构以manifest/per_call/aggregate为准。

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

### E-01 `bench_onnx_embedder_cpu.py` — 嵌入器 CPU 核分配基线

```bash
taskset -c 4-7 .venv/bin/python scripts/benchmark/bench_onnx_embedder_cpu.py \
    --threads 4 --tag a76x4
```

复刻 `OnnxSentenceEmbedder` 的完整形状（CLS pooling、pad/truncate 512、L2
归一、batch 32），量的是**换 CPU 核会怎样**，用来决定 big.LITTLE 主机上
嵌入器该跑在哪几个核。输出单行 JSON（`ms_per_doc`、`query_p50_ms` 等），
无 SLA 门限——它是选型依据，不是回归门。

RK3588 实测：A76×4 = 14.27 ms/doc，A55×4 = 56.76，**8 核全开 = 15.81
反而比只用 4 个大核慢**（ORT 均分工作量，整批等最慢的 A55 线程）。
结论是小核必须**排除**，不是"顺便加上"。`--threads` 要等于绑的核数。

模型目录用 `--model-dir` 或 `BGE_MODEL_DIR` 指定，需含
`onnx/model_quantized.onnx` 与 `tokenizer.json`。

## 升级回归

旧 `run_memory_perf_report.sh` 使用已删除的 sqlite_exact、rules 和三段空间 ID，
现已移除。版本升级验收直接复用完整测试，不再维护第二套启动配置：

```bash
.venv/bin/python -m pytest tests/memory contracts/tests -q
```

性能分析可使用本目录的 `bench_mempalace_chroma_lifecycle.py` 和
`probe_recall_stages.py`；历史报告只代表当时版本，不能作为本轮性能结论。

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

## 不在范围

- CI 跑 bench — 单次 ~10 分钟,不适合 PR gate
- 跨机网络 latency — 全部 localhost
- LLM 失败重试链 — bench 假设 steward LLM 稳定
- chromadb HNSW 调参 — mempalace 上游负责
