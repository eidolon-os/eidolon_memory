# 召回路径核查与取消生命周期修复 — 2026-10-05

## 结论

锁定依赖下的小规模隔离语料没有复现历史 chat p95 445ms。直接并发主题通道的实验未通过，已撤回；最终生产改动只负责收拢召回创建的向量/图谱任务，避免调用取消后图谱任务继续运行。没有新增生产接口、依赖、路由器、缓存层或配置开关。

T1完整验收仍未完成。本报告不是生产性能达标证明，也不支持改变原来的SLO。

## 环境与口径

- 基线 memory commit：`7ff58458`（完整SHA见各JSON manifest）；所有测量诚实记录 dirty=true。最终是工作区 diff，非发布基线。
- Mac arm64、Python 3.13；机器信息与依赖版本见原始JSON manifest。当前checkout版本清单见 `workspace-versions.json`，不代表线上部署。
- 真实 bge-small-zh-v1.5 quantized ONNX，经仓库已有 `eidolon-memory-embedder` HTTP服务，4线程、默认服务并发；模型SHA256见 `model-hashes.json`。
- MemPalace 3.10.0、Chroma向量存储、SQLite KG。使用现有LocalPalaceRouter初始化隔离Realm及管理锁，不直接打开在线服务持有的数据。
- 合成语料：128条事实、8条主题、8条图谱关系；确定性构造见 `seed.py`，查询8类。语料未经过steward，不能测抽取质量或原始用户语料效果。
- 每场景8次预热、160次正式采样（每条查询20次），各阶段before/experiment/after分别6场景；总正式采样2880次。
- warm重复使用查询embedding缓存；uncached每次清缓存，仍复用模型进程，不是模型冷启动。第一轮调用也在per_call中保留。
- chat不带图；chat-graph/voice带图。进程内调用生产召回入口，包含真实本机HTTP embedding，不含Agent、MCP传输、NATS写入和真机音频。
- 原始trace存在重叠，绝不将分段p95相加。统计沿用已有report.percentiles，不另实现一套分位数。

## 结果

| 场景 | 原版 p95 ms | 并发实验（拒绝） | 最终取消清理改动 |
|---|---:|---:|---:|
| chat / warm | 12.532 | 13.461 | 12.568 |
| chat / uncached | 14.833 | 14.909 | 19.570 |
| chat-graph / warm | 14.822 | 14.477 | 19.111 |
| chat-graph / uncached | 17.573 | 16.943 | 17.098 |
| voice / warm | 12.968 | 14.166 | 14.128 |
| voice / uncached | 15.378 | 17.165 | 18.465 |

这是一台机器的单批次开发对照，不能据此声称稳定加速。最终修复没有延迟收益目标；其收益是取消后的任务生命周期正确。

`comparison.json`对比before与最终after全部1008次调用（含预热）的vector storage ID与KG ID，顺序完全相同，均无vector degraded。上述一致性只针对本合成语料；权限、异常和线程锁语义另由回归测试保护。

## 为什么拒绝主题并发

实验补丁完整保留在 `rejected-theme-parallel.patch`，当前生产代码不包含它。

- 锁定依赖下未获得一致性能收益：部分场景略快，部分更慢，不能将单批噪声宣称优化。
- 6种场景中共5次首次调用被标记vector degraded；出现 `RustBindingsAPI ... no attribute bindings` 或palace缓存KeyError；原始日志和预热样本保留在experiment文件中。预热后的结果与原版一致，不能据此忽略首请求失败。
- 机制判断：缓存使用functools.lru_cache，尚未完成的同query请求不会合并；向量和主题同时首次搜索会竞争存储客户端初始化。代码与实验支持这一排查方向，但未单独量化每种争用的贡献。
- 不用“给benchmark加预热”隐藏问题，也不为使并发方案成立而引入全局锁/新缓存协调层。后续若需要并发，先在存储适配器生命周期和query共享能力内形成可验证方案。

## 最终保留的修改

1. `public_recall.py`：try/finally取消并await其创建的向量/图谱任务；保持主题串行、现有图谱降级、排序、预算与可见性策略。LockedBackend继续持有底层worker锁直到真实操作结束。
2. `probe_recall_stages.py`：直接调用生产入口，复用router/trace/manifest/percentiles；增加chat/KG模式、原始结果ID、完整预热样本，出现vector degraded时退出非零。
3. 测试：事件同步验证取消发生在向量、图谱或主题阶段时无遗留子任务，覆盖各通道异常隔离。采用事件因果关系，避免用毫秒阈值断言调度。

## 复跑

在memory子仓运行，先确认uv环境与锁文件一致；`--no-sync`只适用于已核验环境。本轮发现并修正了3.9/3.10不一致。端口18783是本轮隔离服务，不访问线上provider；模型路径按本机缓存调整。

```bash
uv run --no-sync pytest tests/memory/test_mempalace_public_contract.py -q
HF_HUB_OFFLINE=1 uv run --no-sync eidolon-memory-embedder \
  --model bge-small-zh \
  --model-dir /Users/manson/.cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5/snapshots/75c43b069aac4d136ba6bc1122f995fedcfd2781 \
  --host 127.0.0.1 --port 18783 --threads 4
# 另一终端；seed拒绝覆盖已有实验目录，复用原数据时跳过seed。
NO_PROXY=127.0.0.1,localhost uv run --no-sync python benchmarks/results/recall-concurrency-20261005/seed.py
uv run --no-sync python benchmarks/results/recall-concurrency-20261005/measure.py after
uv run --no-sync pytest tests contracts/tests -q
```

首次尝试受沙箱回环端口/连接限制失败；获准后只开启本机隔离服务。初始化曾以local provider被现有配置校验拒绝，已遵循MemPalace公共API改用现有HTTP embedding服务，没有改生产校验绕过限制。

before/experiment由 `run_variant.py` 加载归档的 `variants/serial.py` / `parallel.py`，调用同一个生产入口探针，不覆盖工作区文件。原始测量命令逐份保存在manifest中；`*-sources.json`记录生产源码、探针与配置哈希。`benchmark-settings.json`为本轮生成的无凭据隔离配置。复跑before/experiment只需将measure.py最后的phase参数替换；不要覆盖现有证据，另建实验输出目录。不得混用已更改的线上数据。

## 未覆盖与下一步

- 代表性规模、目标Pi/OPi硬件、MCP延迟、写入确认/可读时效、真实LLM质量未测。
- 小语料通过不替代T1整体验收，不改BENCHMARKS历史表中的数值。
- 下一步先补上述规模和MCP基线，再按trace决定优化位置；T2先做失败分类，复用现有ledger与时间语义，不直接添加四选一路由。

## 验证记录

- 初次完整回归：1323 passed、3 failed、11 deselected，383.35s。失败均源于已安装MemPalace 3.9.0与仓库锁定3.10.0不符。旧依赖原始数据移入 `legacy-mempalace-3.9/`，不能当本版主结果。
- 使用本机缓存执行 `uv pip install --offline --no-deps --python .venv/bin/python 'mempalace==3.10.0'`，只对齐该依赖，没有更改锁文件、生产逻辑或测试断言。
- 对齐后依赖契约/初始化/召回/锁回归38项通过；原定向回归69项通过；修改文件Ruff通过。
- 同一取消测试加载基线与最终实现：基线vector取消阶段失败，最终三个阶段均通过，见 `cancellation-baseline-comparison.json`。
- 3.10.0完整回归：**1326 passed、11 deselected，355.09s**，包括隔离NATS/HTTP E2E与真实存储测试，日志见 `full-tests-310.log`；默认排除llm/live_realm，不代表真实LLM验收。
- `git diff --check` 与本轮修改/新增工具的Ruff检查通过；本轮18783隔离embedding服务已关闭。
