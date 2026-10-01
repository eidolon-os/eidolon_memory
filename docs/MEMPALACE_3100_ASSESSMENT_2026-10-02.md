# MemPalace 3.10.0 升级、验证与产品评估

核验日期：2026-10-02。本次范围为仓库升级和隔离验证；运行主机发布另行安排。

## 结论与版本依据

仓库从 **3.9.0 升级到 3.10.0**。核验时 GitHub 最新正式 release 与 PyPI 最新版本一致，发布时间为 2026-09-16；未采用未发布的主分支或可选 Rust 构建。ChromaDB 仍固定在原锁文件的 **1.5.9**，其他依赖没有随本次升级更新。

依据：[正式发布说明](https://github.com/MemPalace/mempalace/releases/tag/v3.10.0)、[PyPI](https://pypi.org/project/mempalace/)、[3.9→3.10 完整差异](https://github.com/MemPalace/mempalace/compare/v3.9.0...v3.10.0)。对官方 wheel 校验 SHA-256 后读取实现，避免只依据功能标题判断。

依赖升级的直接价值以**存储可靠性和诊断准确性**为主。随后完成三个 P1 接入：独立词法候选、来源与时间展示、自有图谱分页及历史查询，并完成 Mobile 各浏览入口的统一显示与适配。详见下方实现与验证章节。轻量 MCP、Rust exact 等能力仍需专项评估。

## 已完成的仓库适配

1. `pyproject.toml` 与 `uv.lock` 精确固定 `mempalace==3.10.0`。
2. 父进程和初始化子进程统一显式设置 `MEMPALACE_CONFIG_DIR=<service_home>/.mempalace`。新版新安装默认遵循 XDG；仅改变 HOME 不足以阻止外部 `XDG_CONFIG_HOME` 或 `MEMPALACE_CONFIG_DIR` 重定向配置。上游 writer lock 仍使用 `~/.mempalace/locks`，因此继续使用同一个 service HOME。
3. 保持 `mempalaces-v3.9/<memory_space_id>` 与同级 `.ledgers` 原路径。这里的 `v3.9` 是已存在的数据目录 epoch，不是运行依赖版本。改变目录会使已有 Owner Realm 看似丢失；本次不清库、不回填、不重建向量、不清除事实或图谱。
4. 更新公开契约检查、公开 API 探针、Chroma 生命周期探针，并新增只接受解释器参数的跨版本探针。后者无法指定已有 Palace 路径，全部数据在临时目录内合成。
5. 更新当前版本说明；3.9 历史评估文档保留历史语义。

官方 wheel SHA-256：`9f65645235ba4da58fda237e5fad0ba83a5941066a18bcfdbf178b7fc8cf7abf`，与 `uv.lock` 一致。

## 调用检查及实际边界

| Eidolon 调用/职责 | 3.10 检查结果与决定 |
|---|---|
| `get_collection`、`BaseCollection.upsert/query/get/delete` | 显式 document/query vectors、类型化结果和距离契约可用；保留公开 API 适配，无私有 provider/cache 注入。 |
| `get_collection(read_only=True)` | 参数仍可用；**Chroma 不实现独立的原生只读客户端**。继续保留 Eidolon 的读写纪律和锁，不因发布说明的 SQLite WAL reader 优化而去掉它们。 |
| 配置的 drawers collection 与 `mempalace_closets` | 可用。新增对未知 collection 名的拒绝测试；不使用私有 `_skip_name_check` 绕过检查。 |
| audience/device/room/wing 范围 | 继续由 `BaseCollection.query(where=...)` 下推。升级后的真实 NATS→Runner→MCP 召回验证覆盖 Companion、设备和 Realm 隔离。 |
| `search_memories(vector_disabled=True)` | 保留 HNSW 不安全时的公开词法探测。新版返回增加日期来源，但仍缺少通用 audience/device 等权限 metadata；非空结果继续 fail closed，不能直接当作有权限的召回结果。 |
| 公开 backend `close_palace`、HNSW 状态检查 | 调用可用；重开、跨进程写入可见性、快照和进程终止恢复探针通过。 |
| SQLite canonical ledger、KG、删除及重放 | Eidolon 自有职责；不能用上游 drawers/KG/logstream 当作现有事实与隐私协议的替代品。 |
| 上游包拆分 | `mempalace.palace/searcher/cli/mcp_server` 公开导入路径保留；使用公开路径，不直接导入以 exec 加载的实现 fragment。 |

关键源码：[集合入口](https://github.com/MemPalace/mempalace/blob/v3.10.0/mempalace/palace/collection.py)、[公开 collection 契约](https://github.com/MemPalace/mempalace/blob/v3.10.0/mempalace/backends/base.py)、[Chroma 实现](https://github.com/MemPalace/mempalace/blob/v3.10.0/mempalace/backends/chroma.py)、[搜索入口](https://github.com/MemPalace/mempalace/blob/v3.10.0/mempalace/searcher/query.py)、[词法降级结果](https://github.com/MemPalace/mempalace/blob/v3.10.0/mempalace/searcher/sqlite_bm25.py)。

## 新版功能逐项评估

下表中“直接受益”指当前调用经过对应实现；“需接入”指仓库升级没有启用该功能。收益判断是对 Eidolon 当前架构的分析，不是上游性能宣传的转述。

| 新版功能 | Eidolon 产品/技术价值 | 本次及后续决定 |
|---|---|---|
| POSIX 数据库头探测不再释放进程持有的 SQLite/WAL 文件锁 | 减少外部连接 checkpoint/unlink WAL 导致写入丢失的风险；对“说记住了但持久化不完整”的可靠性目标直接相关，但不能据此认定它是上次北京案例的原因。 | **直接受益，最高优先级**。升级并验证生命周期。见 [#2472](https://github.com/MemPalace/mempalace/pull/2472)。 |
| Chroma 管理的系统在缓存失效前被关闭 | 外部 inode/mtime 变化时减少遗留 native client 及资源问题；不覆盖应用自行创建的 PersistentClient。 | **直接受益**；保留公开 backend 管理，已验证跨进程可见性和重开。 |
| Caller-vector Chroma collection 创建/重开时无默认 embedding function，并失败关闭 | 保护显式向量契约，减少隐式下载、模型身份/维度意外变化。 | **直接受益**；512 维显式向量探针通过。不代表可以变更现有 collection 的向量维度。 |
| HNSW 无法 flush、替换 stub、异常 header 计数的识别 | 改善不完整索引的可见性，降低直接打开异常索引的风险。 | **直接受益于当前健康检查**；不自动运行 repair，也不把小库 flush-lag 都当损坏。上游 stranded 判定有 2,000 embeddings 等门槛。 |
| macOS 无 WAL/SHM sidecar 的健康库检查恢复 | 减少把正常冷启动库判作不可读。 | 上游完整性路径受益；Eidolon 自己的健康判断仍需独立保持正确。 |
| repair / integrity 说明判定来源、SQLite 版本、跳过原因；FTS5 错误分类更严格 | 运维界面应区分“已检查且干净”“未检查”“不可读”“损坏”，避免错误重建。 | **值得借鉴**，尤其未来故障诊断页；本次不新增自动修复。 |
| 可选 `rust_exact`、连续向量 buffer、Rayon、释放 GIL；NumPy norm cache/top-k partition | 对 SQLite exact 扫描有潜在 CPU/尾延迟价值；复杂过滤及没有 native extension 会回退。 | **当前 Chroma 不获得这类加速**。未来独立比较 sqlite_exact/rust_exact 与 Chroma，覆盖中文、设备过滤、索引规模、ARM/Linux 和 native wheel。 |
| 独立 native vector CLI | 可用于离线向量检验，不依赖 Python 文本嵌入。 | 接收向量，不负责 embed；不能直接替代现有语音记忆服务。 |
| 搜索和 wake-up 显式只读，SQLite exact WAL reader 避开 writer lease | 有利读写并发。 | Chroma 忽略该原生只读模式；**不解除现有 service 锁**。 |
| 3-tool light MCP 与 PQL | 上游工具 schema 变小，有利面向模型的工具选择与上下文预算。 | **需接入**。Eidolon 使用自有 MCP 工具，不会自动减少 schema/token。未来可参考受限查询语法；必须由服务器注入 scope，禁止模型通过 PQL 扩大 Owner/Companion 权限。 |
| `host:harness:project` shared-brain rules；rules 改为 `--host/--harness/--project`，支持 full/light | 有利开发 Agent 的共享工作记忆和协作身份。 | 当前语音身份是 Owner/Companion/Device/Council；不把工作区身份当作用户授权身份。现有 runtime 不调用已移除的 `rules --agent`。 |
| XDG 新安装配置目录与 hook 兼容修复 | 提高普通桌面安装一致性；可能使服务配置意外偏离。 | **已适配**：显式 pin service config，旧数据路径保持。 |
| CLI daemon direct/prefer/require 写路由与 sweep job | 提供上游 CLI 单 writer 路由；提交后不直接重跑，避免重复。 | 默认仍 direct；当前 NATS、canonical ledger、outbox/DLQ 已承担可靠写入，不新增第二条队列或 writer。 |
| pgvector shared namespace 与断连重试一次 | 多主机可共享表，减少重启后一次连接错误。 | **当前 local Chroma 不受益**。未来云模式要先解决 authority、租约、租户隔离、幂等和备份，namespace 不能替代授权。 |
| 可配置 vector/BM25 混合权重 | 有利评测不同查询类型。 | Eidolon 继续使用既有 RRF，不消费上游权重。已通过公开 `BaseCollection.lexical_search` 接入独立词法候选；复用其索引和 BM25 分数，最后才截取结果。 |
| `last_modified` | 产品可区分首次记住与最后纠正。 | Chroma metadata 初始化可自动补为 filed_at；重新修改时间的语义来自上游 `update_drawer`，不能假定 Eidolon 原始 upsert/update 都自动提供准确纠正时间。需要统一自有 ledger 与投影的时间契约。 |
| 搜索返回 filed_at、authored_at 及来源、content_date 及来源 | 有利回答“什么时候知道”“这段内容讲的是什么时间”，并解释来源可信度。 | 已通过共享 Owner 契约展示已知录入时间、变更时间、事件时间和原话；旧数据缺失保持未知。新写入的 filed_at 与 indexed_at 一致，不再拿事件日期当录入日期。 |
| 上游 KG timeline 的 limit/offset | 解决上游首 100 条限制。 | Eidolon 使用自己的双时间 KG，已在原 Owner graph API 上完成 keyset 分页、历史模式和同一权限过滤，手机可继续展开并识别已结束关系。 |
| 纯读取 `effective_strength()`，避免维护反复累乘衰减 | 可用于长期未提及偏好、话题的召回权重。 | **可参考**。永久事实、有效承诺、用户明确要求记住的资料不可因未提及直接删除；不能把 hallway/tunnel 衰减误说成已经全量启用。 |
| MCP Logstream 无 cursor 默认最新优先，有 cursor 保持升序 | 更适合最近操作展示。 | 自有 sync/canonical 流保持既有 cursor 语义。上游 CLI/Logstream 仍默认最旧优先，不对所有入口做统一推断。 |
| `get_all_rows()`、Qdrant 一次 scroll；status 一次 metadata 读取 | 大规模 Qdrant 列表/统计可减少重复分页。 | **当前 Chroma 不获得 Qdrant 量级收益**。Owner 页面仍要预算与分页，不能改成无限量装入所有数据。 |
| hallway recompute 仅读取本次 mined wing | 减少上游 mining 后关联计算成本。 | 上游 room/wing 结构路径可能受益；手机关系图谱与事实关系提取仍是自有 KG，不由 hallway 计算替代。 |
| DeepSeek Harness plugin：recall、autosave、light MCP | 为开发 Harness 提供会话自动保存/召回范式。 | **当前 LiveKit 语音流程不受益**；适合将来工作 Agent 产品，不混入语音 writer。 |
| CLI/MCP/searcher/palace 代码按域拆分、上游测试拆分 | 上游维护性改善，公开入口保持。 | 公开契约已检查；不依赖实现 fragment 或 monkeypatch 内部全局。 |

### 其余修复与适用范围

发布说明还包含以下修复，已纳入范围判断，但没有为了启用它们扩大本次改造：

- `init` known_entities 读取失败不静默覆盖，采用临时文件/rename；当前初始化路径可能受益，不能把它等同于 Eidolon canonical ledger 的原子性。
- repair-status 区分真正不存在和不可访问；named pipe 在完整性检查前被拒绝，避免卡死。运维诊断值得采用相同状态区分。
- wake-up newest drawers、diary 超过 10,000 条分页、上游 JSON-RPC 错误响应改进；Eidolon 使用自己的公开 recall/MCP 面，不因此自动改变其行为。
- worktree transcript wing 归一、CLI daemon source 转绝对路径、过滤搜索 Error finding id fallback；当前通过自己的 projector/collection 调用，不是相同入口。
- checkpoint 排除 Harness 注入文本、不识别的 Codex rollout 留待解析器更新；未来工作 Agent 导入有益，当前语音来源不使用这些 parser。
- backend 初始化失败与锁竞争分开；serve 等待 writer lease、退出码和清理注册改进；忙 daemon 注册保留、stale PID start/stop 恢复。当前自有 supervisor/Runner/NATS 生命周期不由这些入口管理。
- 清理 PYTHONPATH 保留解释器 site-packages，XDG fresh-install hook kill switch 修复。初始化子进程获得上游修复，但它们不是语音事实 extraction 能力。

## 产品与技术优先级

| 优先级 | 工作 | 验收方式 |
|---|---|---|
| P0 | 单独安排主机发布，备份 Palace 及 `.ledgers`，核对 writer/配置根，完成写入→手机列表→图谱→重启可见性验收 | 不以对话里“记住了”替代持久化与 UI 验收；对一次测试事件跟踪 source_event、canonical 状态和投影。 |
| P1，基础完成 | 给记忆展示来源、首次记录、已知变更、事件时间 | 来源及不同时间已通过共享契约到手机；缺失保持未知。完整修订关系仍以已有 canonical ledger 为准，不能从 filed_at 推测。 |
| P1，接入完成 | 在相同 scope 下增加独立词法候选合并与统一排序 | 精确标识符和权限边界已验证。中文人名/地名/专有词与语义问法的 recall@k、误召回和 P95 留给代表性数据评测。 |
| P1，接入完成 | 自有 KG 分页和历史模式 | 关系跨页完整、权限一致、失效历史可见均已验证；综合故障诊断页可继续发展。 |
| P2 | 参考 light MCP 的工具面收敛 | 在 Eidolon 自有权限边界内测 schema token、成功率、工具调用轮数；不能绕过 scope 或把轻量 schema 当作模型质量提升的证据。 |
| P2 | 偏好/话题按读取时衰减，显式区分永久事实 | 重复读取/维护结果不改变持久数据，不造成永久事实和承诺丢失。 |
| P3 | exact/Rust 或云存储专项实验 | 独立候选环境和代表性数据；证明相对当前 Chroma 的价值后才决定后端扩展。 |

## 继续推进的实现与验证

此轮沿用既有适配器、召回排序、Owner 契约、KG 和手机页面。未新增搜索索引、队列、图谱存储、平行 API 或版本兼容层，也未修改 MemPalace 包源码。

### 原生词法候选进入统一召回

`mempalace_fast_search` 在同一 collection、同一 wing/room/audience/device `where` 下读取向量候选和公开 `lexical_search` 候选，按实际 storage id 合并。纯词法候选没有测得的向量相似度，因此 `similarity=null`，不伪造 cosine 分数。

既有 RRF 使用上游 BM25 分数；最终可见性和上下文排序继续由 `RecallPolicyRegistry` 决定。移除了适配器提前截断以及融合后按原始向量相似度重新排序的行为。关闭既有 `rerank_enabled` 时仍使用向量路径。

真实 MemPalace/Chroma 测试放入 100 条向量近邻噪声、一个向量窗口之外的精确标识符，以及其他伙伴/设备的匹配记录：公开召回前两条包含精确目标，排除越权记录，目标向量相似度保持未知。

原生词法接口的索引和词法能力并非 3.10 新发明；这里复用其已有公开能力。上游将连续中文视为词串，单字查询没有原生候选；无正词法分数时保留已有中文按字 BM25 排序，但它不能增加向量窗口之外的中文候选。本轮没有注入 tokenizer 或新建中文索引，也不据此宣称真实中文模型质量已提升。

### 来源和不同时间通过共享契约到达手机

fragment 和 triple 的 `evidence_quote` 沿既有写入链保存。`MemoryWireRecord.provenance` 统一解释已知时间：`indexed_at` 为首次实际索引时间，`occurred_at` 为已存事件时间，已知 `updated_at/last_modified` 为变更时间。上游初始化为 filed_at 的 last_modified 不当作一次纠正；旧 filed_at 可能是事件日期，不能反推首次知道时间。

共享 `MemoryProvenance` 进入 Owner entries 与完整导出，Admin 只做已有视图投影。OpenAPI、TypeScript 和 Dart 均由现有生成器生成。手机最近记录可点开“记忆依据”，展示原话与已知时间；复制完整副本保留 provenance。缺失日期显示未记录，缺失原话明确说明，不回填真实旧库。

### 原 KG 支持分页和历史关系

在现有 `SqliteKnowledgeGraph.timeline` 增加 `(valid_from, recorded_at, statement_id)` 的稳定 keyset 边界；Owner graph 返回 `next_cursor`、`history` 和关系有效起止时间。每页均重新应用同一 audience 与敏感关系过滤。默认仍为当前关系；历史模式包含已结束关系。

手机沿用图谱画布和关系列表，可继续展开，按 edge id 合并，重新计算已展开节点的度数。切换历史模式从首页重读；下一页失败保留已展开关系并允许重试。没有另建图谱页面或使用上游 KG 替换自有 KG。

### 本轮验证

| 验证 | 结果 |
|---|---|
| Memory 全量非 e2e 回归 | **1,221 passed，65.29 s**，候选 MemPalace 3.10 环境。 |
| scope、隐私生命周期、KG 与 rerank 选定 e2e | **9 passed，99.23 s**；隔离 NATS/Runner/MCP 与确定性测试数据。 |
| Admin 相关服务、接口和契约回归 | **127 passed**；包括生成契约漂移检查。随后增加分页查询参数断言，与跨服务测试合跑 **113 passed**，两组存在重叠，不相加。 |
| 手机接口→Admin→真实 Memory HTTP | **3 项通过**，包含来源/时间、遗忘流程，以及真实 SQLite 图谱 179 条当前关系、180 条历史关系分别跨两页读取。每条 HTTP 答案都由已生成 OpenAPI 校验；没有重复、跨伙伴或敏感关系泄露。 |
| Mobile 记忆页面和请求参数 | **55 项通过**；来源详情、导出、分页展开、失败重试、历史模式、已结束关系及 Companion/cursor/history 参数。 |
| 静态与产物检查 | 修改代码 Ruff、Flutter analyze、三仓库 `git diff --check` 通过。 |

测试只使用临时数据和本地端口。环境代理会接管 loopback 请求，故跨服务测试显式禁用环境代理；启动日志写入临时文件，避免超时诊断阻塞在仍运行子进程的管道上。这是测试隔离，不改变产品 HTTP 客户端策略。

此轮验收证明调用、权限、分页和数据展示契约有效。真实中文模型 recall@k、语音 P95 及大规模混合检索成本仍需代表性数据专项评测；不将合成向量结果当作线上效果或性能承诺。三个 P1 的基础接入已完成，主机发布、light MCP、衰减策略及 Rust/云后端保持独立安排。

## Mobile 整体显示与集成验收

本轮将来源、不同时间和关系历史接入现有手机体验。Memory → 共享 Owner 契约 → Admin → Local API → 生成 Dart 的链路保持单一权威；没有修改 MemPalace 包源码、另建索引/存储、复制后端权限规则或新增平行 API。

| 入口 | 最终行为及复用方式 |
|---|---|
| 记忆时间线 | 原日期分页页面展示完整正文详情，列表仍用短摘要。行上说明是事件时间、记下时间、变更时间或旧记录日期，不再把所有日期称为“最近记下”。最早查询边界为带 UTC 的公元 1 年，包含 1970 年前的日期。无日期记录单独计数，不说成没有记忆。 |
| 搜索结果 | 既有搜索结果携带共享 `MemoryProvenance`，点击进入同一详情。已知首次记下日期与旧记录日期分开表述。 |
| 分类内容 | 点击已有 room 行，使用原完整副本页面及原 export 接口；同一个 `loadCopy` 支持可选 wing/room。筛选由 Memory 应用同一可见性规则后执行，继承选定 Companion，包含无日期记录。读取预算耗尽时明确提示不完整。 |
| 完整副本 | 复用相同列表和详情组件；复制 JSON 包含正文与 provenance。分类页面支持复制当前组，完整副本保留原复制全部行为。 |
| 关系图谱 | 复用现有径向画布、关系列表、分页状态和历史开关。竖屏上下分区、宽屏左右分区；画布先适配可视范围，再使用 Flutter `InteractiveViewer` 缩放/移动。历史关系使用灰线、文字状态和有效起止时间。点击关系进入同一详情组件。 |
| 范围与可读性 | 延续 Owner/Companion 选择；子页面显示正在浏览的范围。概览统计改用 Wrap，详情复用可拖动且可滚动的系统底部面板，适应窄屏、横屏与大字体。 |

共享契约的本轮增量是 `MemoryEntry.value`（避免把摘要当完整正文）、`MemoryRecollection.provenance` 及 export 的可选 wing/room。图谱仍使用既有新增的 cursor/history/valid_from/valid_to 字段。OpenAPI、TypeScript 与 Dart 均用仓库生成器生成，未手工维护另一套 DTO。最终 OpenAPI SHA-256：`09005cf86f5e10f16e1925c8cc119c63f001da0eaea6f57f2ee7ec4e2ed522bd`。

**数据边界：**已有 fragment/triple 记忆记录保存的 evidence_quote 可以展示；旧记录缺失时保持未知。现有 KG statement 不保存逐字原话，关系详情显示已知 recorded_at 与 valid_from/valid_to，并提示原话未保存；没有把规范化事实当作用户原话，也没有在 KG 再存一份来源。完整修订历史仍以 canonical ledger 为准。本次没有回填旧库，手机截图与跨服务测试仅使用合成数据。

### 最终验证结果

| 验证 | 结果 |
|---|---|
| 候选 MemPalace 3.10 环境的 Memory 全量非 e2e | **1,222 passed，66.10 s**；包括本轮分类筛选与搜索 provenance。格式/导入收尾后相关 Owner HTTP **35 passed**，与全量结果重叠。 |
| 前一阶段隔离 NATS/Runner/MCP 验证 | **9 passed**；scope、隐私生命周期、KG 和统一 rerank 的结果保留。本轮手机只读投影没有新增写入通路。 |
| Admin 相关回归 | **116 passed，9.72 s**，包含 4 项跨服务 HTTP 测试。最终契约漂移与跨服务 HTTP 合跑 **12 passed，5.55 s**（8 项契约 + 4 项 HTTP），两轮重叠不相加。 |
| 手机接口 → Admin → Memory HTTP | 分类隔离、完整正文、搜索原话/首次记下日期、公元 1 年查询边界、图谱两页当前/历史关系、遗忘流程通过；公开响应由生成 OpenAPI 校验。真实 HTTP/SQLite KG，记忆存储及遗忘 worker 使用既有确定性测试替身。 |
| Mobile 回归 | **74 passed**；覆盖统一详情、分类与完整副本共用入口、Companion 范围、无日期记录、完整正文、搜索、复制、图谱分页/历史、失败重试和布局。 |
| 显示检查 | 现有暗色主题，320×640、390×844、844×390，文字倍率 1.5；**5 项测试通过**，中文字体截图人工检查通过。该组已包含在手机测试中，不相加。 |
| 静态与生成产物 | Flutter analyze 无问题；修改 Python 文件 Ruff、三个生成器 `--check` 与三仓库 `git diff --check` 通过。Flutter SDK 缓存检查在允许访问缓存后通过。 |

中文截图位于临时目录 `/private/tmp/eidolon-mobile-memory-ui/`，包含 `library-narrow.png`、`graph-phone.png`、`graph-landscape.png`、`relation-phone.png` 和 `relation-landscape.png`。截图使用测试专用字体参数，没有给产品新增字体依赖或演示数据。

本轮完成的是仓库实现、接口契约、隔离测试和显示验收。运行主机发布与真实设备安装按用户已确定的范围另行安排；没有将本机测试等同于真机已发布，也没有据此承诺真实中文召回或语音延迟收益。

## 依赖升级阶段的隔离验证记录

原仓库 `.venv` 保持 3.9.0。候选环境为 `/private/tmp/eidolon-mempalace310-venv`，通过 `UV_PROJECT_ENVIRONMENT=... uv sync --locked --extra dev --python .venv/bin/python` 安装仓库锁文件；测试数据、临时 HOME、HTTP 和 NATS 服务均使用隔离目录/临时端口。未修改运行主机或真实用户记忆。

| 验证 | 结果 |
|---|---|
| 升级前相关路径基线 | 65 passed。 |
| 3.10 公开契约、存储、快路径、Router、Owner entries/graph、Palace 初始化和路径 | 78 passed。 |
| 全量非 e2e `pytest -q tests --ignore=tests/memory/e2e` | 最终 **1215 passed，73.34 s，无失败或跳过**。最终运行允许临时本地端口并显式使用候选 PATH；首次沙箱端口限制及 ruff PATH 缺失已消除。 |
| 允许临时本地端口后补跑 preflight/real_llm_process，加选定 e2e | 18 passed，其中 7 项 e2e 覆盖 scope、隐私 archive/delete/replay、KG recall/invalidate/reactivate。real_llm_process 使用本地模拟 HTTP，不连接线上模型。 |
| 显式候选 PATH 补跑 correctness lint gate | 4 passed，覆盖 eidolon/tests/contracts/scripts。 |
| 更新版本说明后的配置回归 | memory_settings/backend_config 30 passed。 |
| 修改文件 Ruff / diff whitespace 检查 | 均通过。 |
| 公开 API 新库探针 | 3.10.0、Chroma、512 维显式 document/query vectors、write/query/close/reopen 通过。 |
| 跨版本合成库探针 | 3.9 seed→3.10 read/update/delete/add→3.9 rollback-read；原始 metadata 与 audience 过滤保留，SQLite integrity 均 ok。另检查升级前备份可由 3.9 读取。 |
| 生命周期探针 | seed=500，operations=100，最终 521 条；external writer、close/reopen、snapshot、SIGTERM/SIGKILL 后重开通过，SQLite integrity=ok。 |

生命周期样本：read P95 23.61 ms，write P95 17.05 ms，reopen 29.26 ms，external writer visibility 32.56 ms。它们是本机合成 512 维向量的单次结果，**不是与 3.9 的性能比较，也不是线上语音或真实中文 embedding/LLM 质量基准**。

复现命令（从 `eidolon_memory` 目录，使用安装了当前锁文件的候选解释器；`PREVIOUS_PYTHON` 是保留的 3.9 解释器）：

```bash
python -m pytest -q tests --ignore=tests/memory/e2e
python -m pytest -q tests/memory/e2e/test_scope_visibility.py tests/memory/e2e/test_privacy_lifecycle.py tests/memory/e2e/test_kg_admin_pipeline.py
python scripts/probe_mempalace_public_api.py
python scripts/probe_mempalace_upgrade.py --previous-python "$PREVIOUS_PYTHON"
python scripts/benchmark/bench_mempalace_chroma_lifecycle.py
```

测试探针的旧版重开说明合成库格式兼容，不构成线上混用两版 writer 的承诺。后续发布应按停 writer→一致备份→更新→检查→恢复服务安排；不在本次仓库任务里执行。
