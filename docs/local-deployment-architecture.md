# 本地 Memory Node 部署架构

面向 **少量终端 + LiveKit 24/7 陪伴**：读写分离、同机部署、`palace_generation` 跨进程失效、LiveKit 同进程召回。

## 拓扑

```text
LiveKit Agent (同机)
  └─ LiveKitRecallService → PalaceReadSession → MemPalace 读

eidolon-memory-mcp (127.0.0.1:8030) — Admin / IDE
eidolon-memory-worker — JetStream 写 → bump generation → ACK
NATS JetStream — 写缓冲
~/eidolon/mempalace — 单宫殿目录
```

## 写顺序（不可颠倒）

1. Steward → Chroma upsert  
2. `bump_generation()`（`os.replace` 原子写）  
3. NATS ACK  

## 读策略

| 路径 | 行为 |
|------|------|
| **LiveKit** | in-process；`livekit_timeout_seconds` 硬超时；Fail-Fast → `""`；**禁止 sleep 重试** |
| **MCP/Admin** | HTTP；generation 双缓冲；transient 错误可 sleep 重试 1 次 |

## CPU 线程与语音召回优化

启动时由 `eidolon.memory.infrastructure.cpu_env.apply_cpu_thread_env(role=...)` 自动设置 `OMP_NUM_THREADS` / `MKL_NUM_THREADS` 等（若环境未手动 export）：

| 角色 | 策略（Apple Silicon 示例） |
|------|---------------------------|
| **livekit** | 约 2 线程，为音频与 asyncio 留余量 |
| **mcp** | `cores // 4`，上限 4 |
| **worker** | 1，避免与召回抢 ONNX |

YAML 可显式覆盖：`runtime.read.omp_num_threads`（`0`=自动）、`max_wing_parallel`（`0`=自动）。

语音多 wing 召回默认 **一次 query embedding**（`shared_query_embedding: true`），各 wing 并行 `query_embeddings`；`voice_skip_closets: true` 跳过 closets 二次向量查询以压延迟。

## 环境变量

```bash
# 可选：手动覆盖自动 OMP（否则由 cpu_env 按角色设置）
# export OMP_NUM_THREADS=2
export EIDOLON_MEMORY_MCP_TOKEN=...   # 若 MCP 对 LAN 暴露
```

## 启动

```bash
./deploy/dev/init.sh
./deploy/local/run_node.sh
```

## Phase 0 PoC 门槛

```bash
uv run python scripts/poc_chroma_reload_latency.py
uv run python scripts/poc_sqlite_rw_concurrent.py --duration 300
```

Chroma 全量 reload P95 > 600ms → 必须使用双缓冲（已实现 `PalaceReadSession`）。

## 性能报告

```bash
chmod +x scripts/benchmark/run_memory_perf_report.sh
./scripts/benchmark/run_memory_perf_report.sh --full
```

输出：`reports/memory_perf_<timestamp>/summary.md`

## 运维

- 备份：`tar` 宫殿目录（含 `chroma.sqlite3`）
- DLQ：`logs/memory_dlq.jsonl`（Worker 连续 NAK ≥ `worker_max_deliveries`）
- JetStream 上限：`stream_max_msgs` / `stream_max_bytes` / `stream_max_age_seconds`

## LiveKit 集成示例

```python
from eidolon.memory.application.livekit_recall import LiveKitRecallService
from eidolon.memory.application.runtime_warm import warm_palace_read_path
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory
from eidolon.memory.infrastructure.palace_read_session import PalaceReadSession

settings = get_memory_settings()
palace = str(resolve_palace_directory(settings))
await warm_palace_read_path(settings, palace)
session = PalaceReadSession(settings, palace)
recall = LiveKitRecallService(session, settings)

context = await recall.recall_context(
    user_text,
    user_id=user_id,
    session_id=session_id,
)
# 拼进 system prompt → LLM → TTS；写完再 publish JetStream turn
```
