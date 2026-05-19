# 高性能记忆服务架构计划（陪伴场景 · D1 定稿 · 纯净版）

> 仅解决**架构骨架**：进程拓扑、读写分离 / 同步 / 互斥、300ms 读预算、数据完整性、多用户隔离。
> 召回质量、commitment、bi-temporal、reranker、consolidation、eval 等**全部下一计划**。

---

## Context

陪伴智能体（LiveKit pipeline）长期记忆服务。锁定约束：

- **读** = MCP 工具契约（LiveKit 同进程 in-process 调用 / Admin & Claude IDE 走 HTTP 调用同一组工具）
- **写** = NATS JetStream 订阅，agent runner in-process subscriber
- **下层**：读写共用同一个 `LockedBackend` → 同一个 `MemPalacePythonBackend` → 同一个 chromadb `PersistentClient`，单一 `asyncio.Lock` 串行化
- **多用户**：陪伴场景"用户 ≈ agent"，每用户一份 palace + 一个 agent runner 进程（Model A），物理隔离；预期规模 ~10 用户
- mempalace 为项目存在前提，不可抛弃
- 现有损坏宫殿不重建（`mempalace init` 从零开始）

事故诊断与多角度评估后锁定 **D1**：

> **每个 agent 一个独立进程，独占一份 palace；进程内 `asyncio.Lock` 包所有 chromadb 调用；NATS subject 按 agent_id 分支；Admin 也通过同一 agent runner 服务（不再有独立 admin MCP 持有 chromadb 句柄）。**

收益：
- chromadb PersistentClient 回到 tested 路径（单进程独占）→ corruption 根因消除
- 跨进程同步机制（generation 双缓冲）整体**删除**——同进程读写顺序天然一致
- 代码大幅瘦身

代价：跨 agent 共享记忆永久关闭；每 agent 独立 HNSW + ONNX 内存。陪伴单用户场景可接受。

---

## 1. 进程拓扑

```
┌─────────────────────────────────────────────────────────────┐
│  eidolon-memory-agent --user-id=<id> --port=<P>              │
│  （一个用户一个进程；预期规模 ~10 用户）                       │
│  ─────────────────────────────────────                        │
│  ├─ LiveKit pipeline                                          │
│  ├─ in-process recall (MCP 工具 in-process 调用) ──────┐      │
│  ├─ in-process steward                                  │     │
│  ├─ in-process NATS JetStream sub                       │ Lock│
│  │   subject filter: agent.memory.*.<user_id>           │     │
│  ├─ MemPalacePythonBackend × 1  ────────────────────────┤     │
│  │   持唯一 chromadb.PersistentClient                   │     │
│  ├─ control-plane MCP @ 127.0.0.1:P                     │     │
│  │   工具给 Admin / Claude IDE 用                       │     │
│  └─ ─────────────────────────────────────────────────────┘     │
│                                                                │
│  palace: ~/eidolon/palaces/<user_id>/                         │
│      ├─ chroma.sqlite3 (WAL, synchronous=FULL)                │
│      └─ HNSW 索引                                              │
└─────────────────────────────────────────────────────────────┘
```

**关键点**：每份 palace 文件**只被一个进程**碰；进程内**唯一一个** PersistentClient；读 path 与写 path 都进同一把 `asyncio.Lock`。

### MCP = 读 API 契约

- **MCP 是工具契约**（工具名、参数、返回 schema），**不是必须的传输形态**
- LiveKit pipeline 与 agent runner **同一进程** → 直接 in-process await 调用 MCP 工具的 Python async 函数（零开销，省 HTTP framing 5-20ms）
- Admin / Claude IDE / curl → 走 **Streamable HTTP**（127.0.0.1:P）调同一组工具
- 两条路径**同一份代码、同一份 backend、同一把锁**

FastMCP 的工具本身就是 Python async 函数；HTTP 只是表层封装。

### 没有独立 admin 进程

任何外部 chromadb 句柄都不存在。Admin/IDE/Claude 的 MCP client 连具体 user 的 control-plane port → 进入 agent runner 进程内的 MCP 工具 dispatcher → 经过同一 LockedBackend → 同一 PersistentClient。

### Port 分配

`config/users.yaml`：
```yaml
users:
  - id: alice
    port: 8030
  - id: bob
    port: 8031
```

或 CLI `--port` 指定。陪伴个人单机静态分配足够，10 用户 → 10 port 8030-8039。

### 多用户模型选择（Model A）

| 维度 | Model A: per-user palace + per-user 进程 ⭐ | Model B: 单 palace + user_id metadata |
|------|--------------------------------------------|----------------------------------------|
| 数据物理隔离 | ✅ | ❌ |
| 删除某用户 | ✅ `rm -rf palaces/<user_id>` | 复杂 |
| 人格 / LLM context 边界 | ✅ 进程级 | ⚠️ 需 user_id 严格过滤 |
| 一个用户损坏波及其他 | 否 | 是 |
| 内存（10 用户） | ~3GB | ~300MB（共享 HNSW） |
| 跨用户共享记忆 | ❌ | ✅ |

陪伴场景**永远不该跨用户共享记忆**（Alice 的 AI 不能知道 Bob 聊过什么），Model A 的"代价"在你的场景不存在；它的隔离收益完美契合。

### user_id 传递

- agent runner 启动时**绑定**自己的 `user_id`（CLI 参数）
- 进程内一切读写**默认就是这个 user_id**——LiveKit pipeline 不需要每次调 recall 都传 user_id
- NATS subject 已按 `<user_id>` 分支，订阅 filter 后只拿到自己的消息
- payload 内仍保留 `user_id` 字段做防错校验（不一致就拒绝）

### 进程管理：Python supervisor + subprocess（推荐方案 C）

不让用户手动起 10 个进程，也不能 `multiprocessing.fork`（chromadb / SQLite fork 后句柄会撕裂）。采用**Python supervisor + `subprocess.Popen` 启子进程**：

```
launchd / systemd
  └─ eidolon-memory-supervisor                       （唯一系统级注册项）
       ├─ subprocess.Popen → eidolon-memory-agent --user-id=alice   --port=8030
       ├─ subprocess.Popen → eidolon-memory-agent --user-id=bob     --port=8031
       └─ subprocess.Popen → eidolon-memory-agent --user-id=charlie --port=8032
```

**为什么 supervisor + subprocess 而不是 fork**：chromadb PersistentClient 一旦在父进程打开，fork 后子进程共享 SQLite fd，**立刻 corruption**。必须用 `subprocess.Popen` 让子进程是全新解释器，各自 import chromadb 各自开 PersistentClient。

#### Supervisor 职责（**只管生命周期，不碰记忆**）

启动：
1. 读 `users.yaml`
2. 校验 NATS 可达（仅 ping，不订阅）
3. 逐个 `subprocess.Popen(["eidolon-memory-agent", "--user-id", uid, "--port", str(port)])`
4. 捕获 stdout/stderr 到 `~/eidolon/logs/<user_id>.log`

运行：
- 每 5s `poll()` 检查子进程
- 异常退出 → 指数退避重启（1s, 2s, 4s, 8s, 30s 上限）
- 连续 5 次失败 / 60s → 标记 user `degraded`，停止重启，写 alert log

关闭（SIGTERM / SIGINT）：
- 向每个子进程发 SIGTERM
- 等待 graceful exit（30s timeout）
- 超时 SIGKILL
- supervisor 自己最后退出

热加载：
- 监听 SIGHUP 重读 `users.yaml`
- 新增 user → spawn；删除 user → SIGTERM 对应子进程

#### 与"手动单跑"完全互通

- `eidolon-memory-agent --user-id=alice --port=8030` 永远可单独跑（开发 / debug / 单用户）
- `eidolon-memory-supervisor` 是生产入口
- **两者互不依赖**——supervisor 只是 spawn 同一个 CLI

#### 配置位置：主配置文件指向 users.yaml

`memory.default.yaml` 新增字段指向 users.yaml 路径：

```yaml
supervisor:
  users_file: "config/users.yaml"   # 相对仓库根；绝对路径亦可
  # 也支持环境变量 EIDOLON_MEMORY_USERS_YAML 覆盖
```

解析优先级：环境变量 > 主配置字段 > 默认 `~/.eidolon/users.yaml`。

#### users.yaml 示例

```yaml
# 每个用户独立一份 palace + 独立 agent runner 进程
users:
  - id: alice
    port: 8030
    enabled: true
    # palace_path: ""   # 可选 override，默认 ~/eidolon/palaces/alice/
  - id: bob
    port: 8031
    enabled: true
  - id: charlie
    port: 8032
    enabled: false       # 关停：supervisor 不会 spawn；palace 数据保留
```

字段语义：

| 字段 | 必需 | 默认 | 说明 |
|------|------|------|------|
| `id` | ✅ | — | user 唯一标识，与 NATS subject `<base>.<id>` 对齐 |
| `port` | ✅ | — | control-plane MCP 监听端口；启动前检查冲突 |
| `enabled` | ❌ | `true` | `false` 时 supervisor 不 spawn；palace 数据保留，未来翻 `true` 立即恢复 |
| `palace_path` | ❌ | `~/eidolon/palaces/<id>/` | 显式覆盖默认 palace 目录 |

#### supervisor 启动行为

1. 解析主配置 → 拿到 `supervisor.users_file` 路径
2. 读 users.yaml；schema 校验失败 → 拒绝启动并 log
3. 过滤：只考虑 `enabled: true` 的用户
4. 端口冲突 / 重复 id 检查 → 失败拒启动
5. **eager init**：对每个 enabled user，调 `ensure_palace_initialized(user_id, palace_path)`，并发上限 4
   - 不存在 → `subprocess.run(["mempalace", "init", path])`（独立子进程，supervisor 自身不持 chromadb 句柄）
   - 已存在 → 跳过
   - 失败 → log + 标记该 user `degraded`，supervisor 仍继续启动其他用户
6. 逐个 `subprocess.Popen` 派生 agent runner（仅 init 成功的用户）
7. 进入监控循环（poll + 退避重启）

**为什么 supervisor 不做 warm**：warm 会打开 chromadb 句柄。supervisor 持过 chromadb 再 fork 子进程，立刻撕裂 SQLite fd（D1 的反面）。所以分工：
- **supervisor** 只调 `mempalace init` 子进程（**临时**子进程，启动完立刻退出，不留状态）
- **agent runner** 起来后进程内跑 `warm_palace_read_path`（加载 ONNX + 触发 HNSW 加载）

**`--no-init` flag**：开发场景跳过 eager init，依赖 agent runner 内的 lazy init。生产默认 eager。

#### init 责任分工（一个 helper 三处复用）

```python
def ensure_palace_initialized(user_id: str, palace_path: Path) -> None:
    if (palace_path / "chroma.sqlite3").exists():
        return
    palace_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["mempalace", "init", str(palace_path)],
        check=True, timeout=60,
    )
```

| 入口 | init 由谁负责 |
|------|--------------|
| Supervisor 启动 / SIGHUP 新 user | supervisor 调 helper（eager，并发 4） |
| `eidolon-memory-agent --user-id=alice` 单跑 | agent runner 内调 helper（lazy） |
| `deploy/dev/init.sh --user-id=alice` 手动 | shell 脚本调 helper |

三处复用同一个函数，行为统一。

#### SIGHUP 热加载行为

收到 SIGHUP → 重读 users.yaml，对比当前运行集合：

| 状态变化 | supervisor 动作 |
|---------|---------------|
| 新增 `enabled: true` 条目 | **先 `ensure_palace_initialized`**，再 spawn |
| 现有 user `enabled: true → false` | SIGTERM 该子进程（graceful），palace 保留 |
| 现有 user `enabled: false → true` | **先 `ensure_palace_initialized`**，再 spawn |
| 删除 user 条目 | SIGTERM（palace 保留，等运维显式 `rm -rf`） |
| port 变更 | SIGTERM 旧进程 + spawn 新进程 |

新增 user 工作流 = 加一行 `enabled: true` → `kill -HUP <supervisor_pid>` → 进程自动起来。零停机。

#### 不做 lazy spawn 的理由

10 用户 × 300MB = 3GB 内存常驻，**单机够**。Lazy spawn 需要前置路由器判断"哪个 user 来了"再启进程，会引入 30+s 唤醒延迟，破坏陪伴感。如果未来用户数到 50+ 再考虑 lazy。

---

## 2. 读写分离

### 物理
- 每份 palace 文件**唯一一个进程**持有 PersistentClient
- corruption 根因（多进程共享 PersistentClient）从架构上消除

### 逻辑（进程内）
| 路径 | 调用方 | 走到 backend |
|------|--------|-------------|
| read | LiveKit pipeline、Admin MCP tool | `LockedBackend.search` / `.get` / `.get_all` |
| write | NATS subscriber → Steward | `LockedBackend.ingest_fragment` / `.delete` |

都走同一个 `LockedBackend` 实例，共享同一 `asyncio.Lock`。

---

## 3. 读写同步（一致性）

**进程内强一致**：写完 `collection.upsert` 在 Lock release 之后，下一次 await 的 read 立刻可见。

**没有"跨进程同步"概念**——因为不存在第二个进程持 chromadb 句柄。

→ `palace_generation.py` / `PalaceReadSession` / `pop_mempalace_client_cache` / `close_mempalace_palace` / `background_reconcile` **整套删除**。

---

## 4. 读写互斥（Mutex）

### 单一 `asyncio.Lock` 包所有 chromadb 调用

```python
class LockedBackend(MemoryBackend):
    def __init__(self, inner: MemPalacePythonBackend) -> None:
        self._inner = inner
        self._lock = asyncio.Lock()

    async def search(self, *a, **kw):
        async with self._lock:
            return await self._inner.search(*a, **kw)

    async def ingest_fragment(self, fragment):
        async with self._lock:
            return await self._inner.ingest_fragment(fragment)

    async def get(self, *a, **kw):
        async with self._lock:
            return await self._inner.get(*a, **kw)

    async def get_all(self, *a, **kw):
        async with self._lock:
            return await self._inner.get_all(*a, **kw)

    async def delete(self, *a, **kw):
        async with self._lock:
            return await self._inner.delete(*a, **kw)

    async def ingest_text(self, *a, **kw):
        async with self._lock:
            return await self._inner.ingest_text(*a, **kw)
```

关键：**包到所有调用，包括 read**。chromadb 的 read 路径在 SQLite 层并不严格只读（segment compaction、WAL .shm、`embeddings_queue` 等），同进程内并发 read+read 或 read+write 都需要串行化。

### 锁外 vs 锁内边界

| 操作 | 在 Lock 内 |
|------|-----------|
| Query embedding（ONNX 计算） | 否（纯 CPU，不动 chromadb） |
| filter / rank / format | 否（纯 Python） |
| Steward LLM 调用 | 否（30s 量级） |
| `collection.query` / `.get` / `.upsert` / `.delete` | **是** |
| `PRAGMA wal_checkpoint` | **是** |

### 竞争评估
陪伴单用户 + 低写量：
- 写 ~每对话轮 1 次，每次 50-200ms（含 upsert + checkpoint）
- 读 ~每 LiveKit 触发，30-100ms
- 锁等待 P95 < 5ms（基本无竞争）

---

## 5. 300ms 预算

预算口径：LiveKit 进程内调用 `recall_context` 发出 → 拿到 context 字符串。

| 阶段 | 暖路径 | P95 |
|------|--------|-----|
| asyncio.Lock 获取 | <0.1ms | 1ms |
| Query embedding（LRU 命中） | 0 | 0 |
| Query embedding（cache miss） | 30-60ms | 100ms |
| Wing fan-out vector query（共享 embedding） | 30-80ms | 150ms |
| filter / rank / format | <5ms | 15ms |
| **总计** | **~50-100ms** | **~200ms** |

300ms 在 D1 下宽松达标（in-process 无 HTTP 开销）。

### 失败/降级
- `asyncio.wait_for(0.3)` 包 `recall_context` 整体
- 超时 / 异常 → `{context: "", degraded: true}`，永不抛
- **不重试不 sleep**

---

## 6. 数据完整性防御

corruption 根因虽消除，hard-kill 期半写 / chromadb 自身 bug / 外部因素仍要兜底。

### D2 启动 integrity check
agent runner 启动 lifespan：
```sql
PRAGMA integrity_check;   -- 期望 "ok"，否则拒绝订阅 NATS 与对外服务
```
失败 → 进程 log error，不订阅 NATS 不监听 control-plane port；运维介入。

### D3 写期半写防护
- chromadb PersistentClient 启动后 `PRAGMA synchronous=FULL`（写延迟 +30%，陪伴异步写量低，强烈建议）
- `worker.sync_every_n_turns: 5`：每 N 条 turn 跑 `wal_checkpoint(TRUNCATE)`
- 每次 bump 前 `os.fsync(palace_dir_fd)`（macOS APFS 默认不 fsync 目录）

### D4 部署位置约束
启动检查 palace 目录：
- 拒绝 iCloud / Dropbox / OneDrive / Google Drive / Time Machine 实时备份
- 拒绝 NFS / SMB
- 警告非本地 APFS / ext4

### D5 应急恢复脚本（写好备查，本次不用）
`scripts/rebuild_palace_from_jetstream.py`：从 JetStream 头部 replay ConversationTurnPayload 重建任一 agent palace。`drawer_id = sha256(...)` 保证幂等，replay 安全。JetStream 14 天历史 = RPO 14 天。

### D6 周期性快照
`scripts/snapshot_palaces.sh` + launchd / systemd timer：每 6h `tar.zst` 所有 palace 到 `~/eidolon/snapshots/`，保留 24 份（6 天）。snapshot 前必须 `wal_checkpoint(TRUNCATE)`。

---

## 7. Delta to current implementation

### 新建

| 文件 | 角色 |
|------|------|
| `eidolon/memory/entrypoints/agent_runner.py` | 单 user 入口；LiveKit + NATS sub + steward + backend + control-plane MCP 合一；CLI `--user-id` `--port` |
| `eidolon/memory/entrypoints/supervisor.py` | 多 user 入口；读 `users.yaml` → `subprocess.Popen` 每个 agent_runner；监控重启 + SIGHUP 热加载 + SIGTERM 优雅关闭 |
| `deploy/local/launchd/com.eidolon.memory.supervisor.plist` | launchd 注册项（仅一个） |
| `eidolon/memory/adapters/locked_backend.py` | `LockedBackend(MemoryBackend)` 包装层，asyncio.Lock 串行化所有方法 |
| `eidolon/memory/infrastructure/integrity.py` | `PRAGMA integrity_check` / `quick_check` 工具 |
| `eidolon/memory/config/users.py` | `load_users_config(path)` 解析 `users.yaml` + schema 校验（pydantic）；过滤 `enabled: true`；检查 port / id 冲突 |
| `eidolon/memory/infrastructure/palace_init.py` | `ensure_palace_initialized(user_id, palace_path)` helper；三处入口（supervisor / agent_runner / init.sh）复用 |
| `scripts/rebuild_palace_from_jetstream.py` | D5 应急恢复 |
| `scripts/snapshot_palaces.sh` + launchd plist | D6 6h 快照 |
| `config/users.yaml.example`（或合并入 memory.default.yaml）| `user_id → port` 映射 |

### 修改

| 文件 | 改动 |
|------|------|
| [`config/palace_directory.py`](eidolon/memory/config/palace_directory.py) | 加 `resolve_palace_for_user(user_id) → ~/eidolon/palaces/<user_id>/`；启动位置检查（D4）；**lazy init**：目录不存在时自动 `mempalace init` |
| [`config/memory.default.yaml.example`](eidolon/memory/config/memory.default.yaml.example) | `recall.livekit_timeout_seconds: 0.3`；新增 `worker.sync_every_n_turns: 5`、`chromadb.synchronous: FULL`、`supervisor.users_file: "config/users.yaml"`；删除 `runtime.read.*` 双缓冲相关项 |
| [`application/livekit_recall.py`](eidolon/memory/application/livekit_recall.py) | 调用走 `LockedBackend`；超时 0.6→0.3；删除 `background_reconcile`（无 staging）；`PalaceReadSession` 依赖移除 |
| [`adapters/mempalace_python_backend.py`](eidolon/memory/adapters/mempalace_python_backend.py) | 启动时设置 `synchronous=FULL`；保持 API 不变 |
| [`infrastructure/chroma_refresh.py`](eidolon/memory/infrastructure/chroma_refresh.py) | **仅保留** `ensure_sqlite_wal` / `checkpoint_sqlite_wal`；**删除** `pop_mempalace_client_cache` / `close_mempalace_palace` / `is_recoverable_db_error` 等 |
| [`infrastructure/bus/subjects.py`](eidolon/memory/infrastructure/bus/subjects.py) | `MEMORY_CONVERSATION_TURN = "agent.memory.conversation.turn"` 改为 base，publish 时拼 `<base>.<agent_id>` |
| [`infrastructure/nats_stream.py`](eidolon/memory/infrastructure/nats_stream.py) | stream subjects 改 `agent.memory.conversation.turn.>` |
| [`infrastructure/nats/turns.py`](eidolon/memory/infrastructure/nats/turns.py) | publisher 接收 agent_id 拼 subject |
| [`infrastructure/bus/client.py`](eidolon/memory/infrastructure/bus/client.py) | 同上 |
| [`entrypoints/worker.py`](eidolon/memory/entrypoints/worker.py) | 拆 `process_turn_message` 为可被 agent_runner 调用的纯函数；**作为独立 CLI 入口的部分删除**（agent_runner 替代） |
| [`entrypoints/mcp_server.py`](eidolon/memory/entrypoints/mcp_server.py) | 重构为"control-plane 工具集模块"，由 agent_runner 调用 `build_control_plane_mcp(backend)`；不再有独立 main 入口；移除 `eidolon_memory_delete` 写工具（若需要 delete，在 control-plane MCP 内部走同一 LockedBackend，仍是单写者） |

### 删除（**纯净化**）

| 文件 / 模块 | 删除理由 |
|-------------|---------|
| [`infrastructure/palace_read_session.py`](eidolon/memory/infrastructure/palace_read_session.py) | 双缓冲机制只为绕开"多进程共用 PersistentClient"——D1 下根本不存在 |
| [`infrastructure/palace_generation.py`](eidolon/memory/infrastructure/palace_generation.py) | generation 文件用于跨进程感知——D1 下没有第二个进程 |
| [`infrastructure/mcp_http_client.py`](eidolon/memory/infrastructure/mcp_http_client.py) | LiveKit 不再走 MCP HTTP；admin 直接连 agent runner 的 control-plane |
| [`application/memory_service.py`](eidolon/memory/application/memory_service.py) | 旧 NATS RPC 兼容层（README 已标 legacy） |
| [`entrypoints/server.py`](eidolon/memory/entrypoints/server.py) | 旧 NATS MemoryService 入口（同上） |
| [`server/`](eidolon/memory/server/) 目录 | 同上 |
| [`infrastructure/cpu_env.py`](eidolon/memory/infrastructure/cpu_env.py) 中按 role 分支 | 只剩一种 role（agent），简化为 `apply_cpu_thread_env_for_agent()` |
| 配置项 `runtime.read.max_wing_parallel` / `search_executor_threads` / `generation_path` / `hierarchy_cache_seconds` / `background_reconcile` / `double_buffer_staging` | 双缓冲已删，全部失效 |
| 测试 [`tests/memory/test_chroma_refresh.py`](tests/memory/test_chroma_refresh.py) 中针对 `pop_*` / `close_palace` 的用例 | 对应函数已删 |
| `pyproject.toml` 的 console-scripts：`eidolon-memory-mcp`、`eidolon-memory-worker` | 合并为 `eidolon-memory-agent` |

### 不动

| 文件 | 原因 |
|------|------|
| [`domain/`](eidolon/memory/domain/) 全部 | 纯 schema / port 定义，不动 |
| [`application/steward/`](eidolon/memory/application/steward/) 全部 | 业务逻辑层，nice as is |
| [`application/recall.py`](eidolon/memory/application/recall.py) / `recall_filters.py` / `public_recall.py` | 召回逻辑层 |
| [`adapters/fake_backend.py`](eidolon/memory/adapters/fake_backend.py) / `search_payload.py` | 测试 + payload 解析 |
| [`application/query_embedding.py`](eidolon/memory/application/query_embedding.py) | LRU embedding cache |
| [`application/runtime_warm.py`](eidolon/memory/application/runtime_warm.py) | warmup，agent_runner 启动时调 |

---

## 8. 初始化策略

**Lazy 默认 + 显式 batch 二合一**：

| 触发 | 行为 |
|------|------|
| Lazy（默认）| agent runner 启动时检查 `~/eidolon/palaces/<user_id>/` 是否存在；不存在则**自动** `mempalace init`，然后跑一次 `runtime_warm`。新用户直接 `eidolon-memory-agent --user-id=charlie` 就工作。 |
| 显式 `deploy/dev/init.sh` 不带参数 | 沿用现有行为：基础环境检查、依赖、目录骨架。不创建任何 user palace。 |
| 显式 `deploy/dev/init.sh --user-id=alice` | `mempalace init ~/eidolon/palaces/alice/` + 一次 warm 调用。运维场景预热。 |
| 显式 `deploy/dev/init.sh --user-id=alice,bob,charlie` | 批量。逐个 init + warm。 |
| 显式 `deploy/dev/init.sh --all` | 读 `users.yaml`，对所有声明的用户初始化。 |

陪伴单机自用：直接跑 agent runner，lazy 接管。多用户部署：boot 时 `init.sh --all` 预热，避免首次 recall 等 init。

---

## 9. 落地分阶段（~8 天）

| 阶段 | 任务 | 工作量 |
|------|------|--------|
| 0 重置 | `mv` 旧损坏宫殿 → `.corrupted.<ts>` | 5 分钟（不预创 palace，lazy 处理） |
| 1 删除 + 路径与配置 | 删 §7"删除"列表所有项；加 `resolve_palace_for_user` + lazy init；`users.yaml` schema；NATS subject 改造（subject hierarchy `<base>.<user_id>`） | 1.5 天 |
| 2 锁 + Backend | `LockedBackend` 实现 + 单测；接入 `MemPalacePythonBackend`（含 `synchronous=FULL`） | 1 天 |
| 3 Agent runner | 新入口合一 LiveKit + NATS sub + steward + backend + control-plane MCP；warm 硬门控；双层超时；`--user-id` `--port` CLI | 2 天 |
| 3.5 Supervisor | `eidolon-memory-supervisor` 实现：`users.yaml` 读取 + `subprocess.Popen` 派生 + 重启回退 + SIGHUP 热加载 + SIGTERM 优雅关闭；launchd plist | 1 天 |
| 4 init.sh 扩展 | 加 `--user-id` / `--all` / batch 支持（与 supervisor 互补，调试/预热用） | 0.5 天 |
| 5 守门 | D2 integrity_check + D3 fsync dir + 周期 TRUNCATE + D4 部署位置检查 + D6 6h 快照 | 1 天 |
| 6 应急脚本 | `rebuild_palace_from_jetstream.py` 备查 | 1 天 |
| 7 验收 | V1-V13 跑齐 + baseline 报告 | 1.5 天 |

**总计 ~9.5 天**。

---

## 10. 验收

| # | 项 | 标准 |
|---|----|------|
| V1 | 暖路径 recall P95 | ≤ 200ms |
| V2 | cold + 跨查询 P95 | ≤ 300ms |
| V3 | 同 agent 内并发 read+write 不退化 | P95 ≤ 320ms |
| V4 | 写并发下读 P99 退化 | ≤ 15% |
| V5 | 写后可见 | 同进程内 < 10ms |
| V6 | warm 未完成 control-plane port 不监听 | curl 失败 |
| V7 | 300ms client 端硬超时生效 | mock 慢 backend |
| V8 | 进程内 chromadb 调用全在 LockedBackend 内 | 静态 grep + 单测 |
| V9 | integrity_check 失败拒接 NATS | 人工 corrupt 一份测 |
| V10 | synchronous=FULL 吞吐 | ≥ 当前 70% |
| V11 | 强 kill agent runner（含 kill -9）重启数据完整 | 模拟 5 次 + integrity ok |
| V12 | 6h 快照可解压并启动 | 恢复演练 1 次 |
| V13 | 7×24h 长跑 integrity 始终 ok | bench 跑一周 |

P50/P95/P99 落 `reports/architecture_d1_baseline_<ts>/summary.md`。

---

## 11. 风险与回滚

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| `LockedBackend` 漏包某个 chromadb 调用 | 中 | corruption 风险残留 | V8 静态 grep；单测覆盖每个 method |
| agent runner 内 control-plane MCP 与 LiveKit 锁竞争 | 低 | Admin 操作期间 recall 偶发卡顿 | 陪伴单用户，admin 操作是手动罕见事件；监控 `lock_wait_ms` |
| NATS subject hierarchy 改造影响历史 publisher | 中 | 旧 publisher 发到旧 subject | 改造期 publisher 双发；JetStream stream subject 兼容 |
| port 冲突（多 agent 部署） | 低 | runner 启动失败 | `agents.yaml` 静态分配 + 启动检查 |
| 单 agent 内存 ~300MB | 低 | 多 agent 时压力 | 单 agent 部署期无虞 |

回滚策略：
- D1 改造作为 `v0.3.0` release；palace 目录加 `version.json` 标识
- 回滚到 `v0.2.x` 需要把 palace 移回 `~/eidolon/mempalace/`（旧路径）
- 因为 D1 是从零开始，回滚等同于"放弃 D1 期间所有数据"——可接受（陪伴个人单机）

---

## 12. 不在本计划范围（下一计划）

- 召回质量：hybrid（BM25+dense+RRF）、reranker、bi-temporal、实体规范化
- 陪伴语义层：commitment memory_type、emotion 跨会话延续、core memory pin
- 评估：陪伴特化 eval set
- 长期可维护：consolidation worker、salience 衰减、embedding 模型版本字段
- 跨 agent 共享记忆（届时考虑写 mempalace 第三方 backend 走 chromadb server 模式或 LanceDB）
