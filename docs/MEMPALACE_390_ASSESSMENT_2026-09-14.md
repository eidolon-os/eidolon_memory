**MemPalace 3.9.0 升级与现有实现优化计划（修订版）**

更新时间：2026-09-14。代码分析基线：`a250317`。本文件替代同日初版方案；下面的范围和执行顺序为当前计划。仓库升级、重构和测试已执行；实际环境清库/发布仍需确定目标主机和状态目录，详见第九节。

**本轮决定**

1. 将 MemPalace 固定升级到 **3.9.0**。旧记忆数据可以删除，不做 3.8 数据兼容、迁移、回填或旧布局自动收养。
2. 对已有机制逐项评估：公开 API 能完整承接且减少复杂度的，就改用上游；上游不满足当前契约的，保留必要的自有实现。优化必须有具体删除项和行为验收，不以新建抽象层或减少文件数为目的。
3. 用户第 3 点原文为“有的话也不上”，已请求澄清。在收到更正前，按字面执行：**发现能力缺口只记录，不纳入本轮实现**。尤其不把双路召回、证据卡、保存进度界面或新图谱体验悄悄包装成重构。
4. 本轮交付为“升级依赖＋清理历史包袱＋优化已有行为＋验证”；初版中的产品功能扩展移出执行范围。
5. 用户追加要求：**补充更充分的单元测试、真实存储集成测试和端到端测试，同时清理过期或不再适用的测试**。这是本轮正式交付项，随代码改动完成，不留作升级后的可选工作。

正式版依据：GitHub 和 PyPI 的最新版本均为 3.9.0，2026-08-31 发布，非预发布、未撤回。调研基线的 `pyproject.toml`、`uv.lock` 和 `.venv` 均为 3.8.0；本轮已统一更新至 3.9.0。[GitHub 发布](https://github.com/MemPalace/mempalace/releases/tag/v3.9.0)、[PyPI 元数据](https://pypi.org/pypi/mempalace/json)

**一、源码复核后的判断**

这次没有出现一个可以整体替代 Eidolon 适配层的新 Python SDK。对两个版本的 AST/签名逐项比较发现：`QueryResult`、`GetResult`、`BaseCollection` 的公开方法，以及 `get_collection`、`get_backend_for_palace`、`search_memories` 的参数，在此次比较范围内一致。类型化结果、显式向量、`close_palace`、词法查询接口在 3.8 已经存在；本次优化是把项目收敛到 3.9 支持的公开契约，不能都宣传成 3.9 新能力。[公开存储契约](https://github.com/MemPalace/mempalace/blob/v3.9.0/mempalace/backends/base.py)、[集合入口](https://github.com/MemPalace/mempalace/blob/v3.9.0/mempalace/palace.py)

3.9 真正与现有代码相关的变化主要是搜索分数/补全语义、上游 MCP mutation 的 closet 清理、图统计，以及配置可靠性。Hub 并发、CLI 转发、logstream 和任务交接有新增，但我们没有通过这些上游入口提供现有服务。[完整差异](https://github.com/MemPalace/mempalace/compare/v3.8.0...v3.9.0)

| 当前机制 | 3.9 上是否有更合适的实现 | 本轮决定 |
|---|---|---|
| dict/typed 混合结果解析，搜索中丢弃存储 ID | `QueryResult/GetResult` 已有 IDs、元数据和向量距离 | **优化**：存储边界直接转成内部记录，清理兼容解析 |
| 单 wing、多 wing、offline 分开的搜索准备和转换 | `BaseCollection.query(query_embeddings=…, where=…)` 能承接共同部分 | **优化**：一个内部执行管线，保留现有对外方法 |
| 原始 similarity 与 closet 排序加权混用 | 上游 3.9 已纠正此语义 | **优化**：对齐原始分数，内部独立保留排序信号 |
| Router 的关闭流程只显式关闭 KG 后释放锁 | `get_backend_for_palace(...).close_palace(path)` 可释放向量客户端 | **优化**：明确按 space 关闭向量句柄，最后释放所有权 |
| 旧 ledger 目录自动搬迁、旧 Chroma 查询兜底 | 本轮不再接纳旧数据，固定公开查询契约 | **删除历史兼容**，不保留迁移路径 |
| 宽泛异常后重试同一查询或返回空集 | 上游有结构化异常类 | **优化**：错误在边界映射，故障不能伪装成正常空结果 |
| 写优先读写锁、取消后后台操作继续持锁 | 上游锁是 MCP server 的私有同步 `_RWLock` | **保留**：我们的 asyncio/取消与跨 vector、KG 保护语义不同 |
| 单 space 与单 palace 所有权 | 上游有 palace 写锁，但不管理我们的 JetStream consumer 身份 | **保留两个所有权维度**，不因清旧版本就删其中一把锁 |
| 一次 query embedding，多 wing 共享 | 上游 `search_memories` 仍不接收外部 query vector、任意 scope where | **保留算法编排**，在其下直接使用公开集合 API |
| 自有 KG、canonical facts、证据、遗忘 tombstone | 上游图和 daemon 不承接我们的全部生命周期与可见性契约 | **保留**；不引入第二套事实源或队列 |
| HNSW 预检 | 当前已经调用公开 `hnsw_capacity_status` | **保留薄包装**，不再叠加一层扫描/缓存 |
| SQL 完整性校验 | Chroma 的公开 `health()` 主要判断 backend 是否关闭；detect 主要检查文件头 | **保留实质校验**；不能用更短的调用换掉语义 |
| 房间导航图输出 | `general` 和新统计字段会直接进入当前调用结果 | **做兼容验证**；不新增图谱页面或显式 tunnel 渲染 |
| 初始化与 embedding 身份 | 当前公开 API＋显式向量方式仍适用 | **保留必要初始化**，不引入私有 provider 注入 |

关键代码：[适配器](/Users/manson/ai/eidolon/eidolon_memory/eidolon/memory/adapters/mempalace_python_backend.py)、[快路径](/Users/manson/ai/eidolon/eidolon_memory/eidolon/memory/adapters/mempalace_fast_search.py)、[Router](/Users/manson/ai/eidolon/eidolon_memory/eidolon/memory/adapters/local_palace_router.py)、[并发锁](/Users/manson/ai/eidolon/eidolon_memory/eidolon/memory/domain/space_lock.py)、[上游 Chroma 实现](https://github.com/MemPalace/mempalace/blob/v3.9.0/mempalace/backends/chroma.py)。

**二、确定实施的优化**

**O1：直接消费类型化结果，保留真实存储身份。**

现有链路是 `BaseCollection.query → _score_results 的 dict → parse_search_tool_payload → MemoryWireRecord`。其中 `_score_results` 没使用 `QueryResult.ids`，parser 又把 `key` 设成 room；`_raw_search_text` 和 `_drawer_content_text` 留有反推旧 payload 的历史说明，但本次检索未发现 `_drawer_content_text` 的调用者。

目标链路为 `QueryResult/GetResult → 单一存储结果转换 → MemoryWireRecord`。真实 drawer ID、完整 source_file、原始 metadata 和原始 distance 全程保留；不要从文件 basename、JSON 重新序列化或正文 hash 反推已经存在的 ID。现有写入使用的确定性 drawer ID 生成规则仍保留，不能误删成“旧兼容”。

具体工作：

- 快路径输出保留存储 ID；内部可以使用 `_storage_id` 一类字段，避免直接改变外部 `key` 的已发布语义。
- 在 adapter 内统一 get/query 转换；保留当前 `value` 类型及 JSON 内容语义，不能把“减少 JSON 往返”误做成改变返回值类型。
- 对固定版本使用 `QueryResult/GetResult`；删除仅服务旧 dict 返回的 `_ids/_documents/_metadatas/_nested` 双形态分支，相关测试桩改成真实类型。
- 审计 `parse_search_tool_payload` 的所有调用，先移出生产存储路径；对仍有真实外部使用的边界独立保留，否则删除其模块和导出。测试中的使用不等同于必须保留生产兼容。
- 删除确认无调用者的正文反推辅助函数与旧版注释。不是全仓库批量删除所有带 legacy 字样的代码：配置兼容和 wire 契约不自动属于旧数据兼容。

验收：ID 与原始 source/metadata 无损；JSON 和文本结果维持原契约；同名文件、同主题不同事实不混淆；租户/设备/敏感信息过滤结果不变。

**O2：单一查询执行管线，修正分数语义。**

单 wing 搜索委托共同的 scoped query 实现；offline 模式只替换 embedding 来源，共用过滤、集合查询、结果解析和错误映射。保留现有一次 query embedding、按 scope 查询和语音 skip closets 行为。

`similarity` 从原始向量距离计算；主题加权只改变内部排序。同步更新快路径最终排序和 `rank_records_by_similarity` 等实际消费者，不能只改一处公式后让后续重新排序把加权抹掉。原始相似度不是事实正确率，不新增对用户的“置信度”展示。[上游分数修复](https://github.com/MemPalace/mempalace/pull/2399)

此次不新增独立 lexical 候选流，也不改现有 BM25＋RRF 为 union；那会改变候选集合，属于 G1 缺口工作。HNSW 已确认不安全时的现有 BM25 降级作为单独入口保留，不能先打开有风险的 Chroma collection 再尝试降级。

验收：同一 query 的语义 embedding 只执行一次；现有正常/语音/降级行为都有明确测试；原始分数不随 closet boost 变化，内部排名仍反映加权；下游过滤不把来源不明的分数当作可信证据。

**O3：关闭资源时，真正关闭 palace。**

`LocalPalaceRouter.aclose()` 当前显式关闭 KG，然后释放进程锁；没有调用向量 backend 的 `close_palace`。上游文档和实现明确：仅丢弃引用/字典缓存不等于释放 Chroma native 文件句柄。

通过基础设施/adapter 的显式关闭职责接入 `get_backend_for_palace(path).close_palace(path)`，上层依赖关闭协议而不是 import mempalace。按每个 palace 关闭，不能调用全局 `backend.close()` 影响其他 space。

关闭顺序：停止准入和订阅 → 等待实际存储操作结束 → 关闭向量、KG 以及实际持有的 ledger 资源 → 释放 space/palace 所有权。重点处理 `asyncio.to_thread` 在调用方取消后仍继续执行的情况：`LockedBackend._operations` 不是可以提前丢弃的装饰。如果操作无法结束，不能一边释放锁一边让后台线程继续碰数据。重复关闭应安全；初始化失败也走对应清理。

验收：同进程 open/close/reopen；关 A 后 B 正常读写；关闭等待在途任务；关闭失败不提前释放所有权；初始化失败后可以重试。资源计数、句柄释放实测后才宣称降低内存。

**O4：去掉旧数据兼容与吞错兜底。**

- 删除 `_adopt_ledgers_beside_the_palace` 和调用，启动直接采用 sibling `.ledgers` 布局。
- 删除 `_merge_tenant_queries`；固定版本支持的查询失败应显式报错，不能再次执行相同查询、吞异常并返回空集。
- `get_by_source_turn_id` 使用固定支持的 `$and` 查询；不再因任意异常自动扩大为来源查询。确有容错必要时只捕获明确的能力异常，并保持 scope 语义。
- `_sanitize_name/_sanitize_content` 直接遵守上游公开校验，不能在其拒绝内容后用 `.strip()` 把内容重新放行。保留我们自己的错误类别映射。
- 收敛仅为已移除 backend 服务的不可达 artifact 分支和 JSON marker 检查，但保留实际 SQLite 表结构/完整性检查。
- 删除与上述兼容路径一一对应的旧测试；改成固定 3.9 契约、合法输入、非法输入和失败可见性的测试。仍用于未来运行恢复、快照、隐私删除的逻辑不因旧数据清空而移除。

验收：空库确实返回空，数据库故障返回失败；新写入 metadata 完整；同一配置错误只产生一个清晰错误，不隐藏为搜索无命中。对 sanitize 行为变化给出失败用例，避免新版本静默拒收现有合法文本。

**三、为何不整体换成上游实现**

- **搜索：** 3.9 `search_memories` 的签名依然没有 `query_embedding`、通用 `where`、audiences 和 device_id。它会自己组织 embedding/closets，且返回摘要化字段，不能完整保留我们需要的自定义 metadata。因此保留小而明确的应用编排，复用其下的公开存储 API。3.9 的 CLI `search(collection=...)` 是另一条入口，不能拿它的 collection 参数当作 `search_memories` 已支持同样能力。
- **锁：** 上游 `_RWLock` 位于 MCP server、属于私有同步实现，不是可复用的 asyncio 锁库。我们还要保护 KG、取消后的线程任务和逻辑 space。不会为了复用导入其私有 server 全局状态。
- **健康：** 当前 `run_integrity_check` 已把 missing 判为失败；上游 MCP 的“缺库不报健康”修复不构成我们重新实现状态 API 的理由。首次创建前文件不存在与服务运行中缺失要分别处理。
- **初始化：** `mempalace init` 与 materialization 的两步值得长期简化，但直接删 CLI 配置步骤是否影响 wing/room 配置、锁目录和 embedding identity 尚未得到等价性证据。本轮保留，避免把不确定的初始化重构混入版本升级。
- **隔离和事实：** 保留 space 身份锁与 palace 路径锁；前者还保护 durable consumer，后者防不同 space 映射同一目录。保留自有 KG、canonical ledger、证据、tombstone、NATS 和模型子进程；3.9 没有把这些职责合成一个满足现有契约的组件。

**四、3.9 可帮助解决的缺口：只记录，当前不实施**

以下状态遵从第 3 点原文。若用户确认是“有的话也补上”，可将对应条目转入明确实施阶段，不能在此之前扩大范围。

| 编号 | 当前缺口/差异 | 3.9 支持与边界 | 当前决定 |
|---|---|---|---|
| G1 | BM25 只重排向量已命中的结果，不能救回被向量漏掉的词 | `lexical_search`＋MCP union 可提供独立词法候选；中文分词、权限下推和耗时仍需我们验证 | 不新增 union，保留现有候选策略 |
| G2 | 我们 drawer mutation 不经上游 MCP，未见 closet 清理 | 3.9 MCP handler 清理旧主题索引；不是 `BaseCollection.delete/update` 的内建级联 | 记录，不新增级联模块；重置清掉旧 closets，也不启用新的 closet 生产路径 |
| G3 | palace 图按房间共同 wing 生成边，未消费显式 tunnel 边 | 3.9 增加显式 tunnel 统计，不等于我们的绘图代码自动获取边 | 只验证现有返回和统计变化，不加新导航能力 |
| G4 | 上游新状态字段不出现在我们的 MCP/客户端 | update awareness、logstream 可帮助运维；不能替代当前 command/decision ledger | 不增加版本提醒、任务事件流或保存进度 UI |
| G5 | 现有服务评测尚未接上新版私有 palace 算法比较 | 上游受控 vector/BM25 路径要求 sqlite_exact；我们的 backend 是 Chroma | 用现有服务基准做本轮回归，不移植完整新评测平台 |

G2 需要明确其含义：本次没有检查生产 palace，尚未证实旧主题明文残留。全量重置移除了历史数据问题，但并不会为未来新增 closet 写入提供级联保证。实施 O1/O2 前应核对本轮允许的生产写路径没有生成这种派生项；若发现当前活跃路径确实生成 closets，应把发现与范围冲突明确报告，不能把禁用 skip_closets 当作硬删除完成。[上游 mutation 修复](https://github.com/MemPalace/mempalace/pull/2355)

对 G1/G5 的文档说明：上游 `PRIVATE_PALACE.md` 仍写 union 未通过 MCP 暴露，但 3.9 发布包的 schema/handler 已含 `candidate_strategy`。采用源码判断，不把文档措辞当作能力缺失。[MCP 实现](https://github.com/MemPalace/mempalace/blob/v3.9.0/mempalace/mcp_server.py)、[评测说明](https://github.com/MemPalace/mempalace/blob/v3.9.0/benchmarks/PRIVATE_PALACE.md)

**五、旧数据清空与重新开始的操作设计**

用户已授权删除旧记忆数据；执行时无需再为这些已确定范围重复请求许可。删除前的路径与资源清单用于准确定位，不是增加一次审批流程。

不能只把 `mempalaces-v3.8` 换成 `mempalaces-v3.9`：旧 `.ledgers`、NATS 待处理 turn/cmd/sync、离线 sync 批次或自动恢复任务都可能把历史事实写回来。清空必须覆盖整个目标 memory space 的重放来源。

顺序如下：

1. **定位实际资源。** 列出配置/环境覆盖后的 palaces_root、各 space 的 palace 与 sibling ledgers、对应 JetStream subjects/consumers、离线同步来源。不能只按默认目录猜；路径需规范化，排除空路径、符号链接越界和其他项目数据。
2. **停止本轮目标的读写入口和重启管理。** 包括 supervisor 的自动拉起、runner、consolidator、生产者和离线重放。等待在途任务结束，确认旧进程不再持有文件；尚有任务运行时不删锁文件或数据库。
3. **清理目标 memory 状态。** 删除旧 palace（含 HNSW、closets、embedding identity 等）、对应 KG 与六类 ledger。清理目标 space 旧消息和 consumer 状态，不能删除整个共享 NATS 数据目录或其他业务 stream。待处理 cmd/sync 与 turn 同样处理。
4. **处理迟到的离线数据。** 清理目标客户端的 memory 同步待发批次、重放检查点，关闭历史恢复/回填。未完成更新或清队列的离线设备暂不恢复写入口。仅将 consumer 起点设成“现在”不能阻止旧事件稍后重新发布；本轮不另造一套代际协议，把生产者和队列切换纳入一次停写操作。
5. **启动新的空库。** `PALACE_STORAGE_EPOCH` 更新为 `mempalaces-v3.9`，同步显式 root 配置、部署环境和帮助文本，保证不会继续指向旧路径。旧目录不自动收养；只新建当前模型身份的集合与当前 schema 的 ledgers。
6. **验证后恢复流量。** 首次启动为空，新 turn 可写且可召回；服务重启不丢新事实；旧队列/同步批次不会自动复活历史事实。通过后恢复生产者和同步。

边界：删除的是本项目旧 memory 数据与其待重放副本。保留 Agent 通用聊天记录、凭据、模型缓存、用户/companion 身份及其他业务数据；这些原始历史不得自动重新导入 memory。不要删除整个 `EIDOLON_STATE_ROOT`、用户 home 或共享 NATS 根目录。

不做旧数据迁移、旧 schema 兼容和强制快照恢复。发布失败时暂停流量修正代码，必要时回退代码；**本轮授权清旧数据不代表可以再次删除上线后新增记忆**。新增事实/删除状态需保全，不能用恢复旧库作为回滚办法。

**六、可执行任务清单与验收**

| 阶段 | 工作项与文件范围 | 完成条件 |
|---|---|---|
| P0，0.5–1 日 | 依赖固定 3.9.0；更新 `uv.lock`；将旧版本命名的现行契约/探针改成当前版本或中性名称；保留历史 benchmark 记录 | 锁文件可复现，运行时报告 3.9.0；传递依赖差异有记录 |
| P1，1–2 日 | O1/O2：`mempalace_python_backend.py`、`mempalace_fast_search.py`、`search_payload.py`、`recall_ranking.py` 和受影响测试 | 一条类型化查询链路；原始 ID/metadata 保留；分数与排名分离；没有引入新候选来源 |
| P2，1–2 日 | O3/O4：Router/关闭职责、旧目录搬迁、旧查询兜底、上游校验与错误映射 | 真正释放向量句柄；重复关闭安全；停止后无后台存储访问；不存在故障变空结果 |
| P3，0.5–1 日 | `palace_directory.py`、settings、部署说明及重置操作清单；现有图输出验证 | root 统一到新 epoch；旧重放来源已处理；图统计口径明确 |
| P4，1–2 日 | 真实 3.9 空库、MCP/NATS 新数据链路、并发/资源与目标设备回归，随后执行停写清旧数据及发布 | 下述门槛通过；无旧数据兼容任务；G1–G5 不混入发布 |

上表代码与基本验证初估 **4–8 个工程日**。本轮追加的测试夹具修复、深入场景覆盖和旧测试清理预留 **2–3 个工程日**，合计调整为 **6–11 个工程日**，按熟悉代码的一名工程师计算；设备/生产者协调时间另计。不是实际测试测出的工期。P1/P2 的代码可在生产数据不变时完成；真正停写和清数据放到代码、依赖包及验证就绪后执行。

验收门槛：

- **固定版本与新库：** 完整目标依赖环境，而不只是 wheel 覆盖；新 palace 初始化、显式 document/query 向量、维度/身份匹配、持久化重开。移除只为旧版本/旧布局服务的测试，不移除当前公共行为测试。
- **行为等价：** 单 wing/多 wing、normal/voice、文本/JSON、权限和设备可见性、KG 融合、现有 BM25 重排、HNSW 降级。分数修正属于预期变化，单独列出排序/阈值断言。
- **生命周期：** 在途查询被调用者取消后仍持锁；关闭等待真正执行结束；close A 不影响 B；同进程重新打开；初始化失败后释放已获取的资源。不存在“锁已释放、线程仍在写”。
- **现有写删语义：** 新库上执行 turn→decision→canonical ledger→drawer/KG→recall；验证更新、失效、硬删除、重放幂等和部分失败补偿。它们是已有能力的回归，不是新增 feature。
- **重置有效性：** 使用可丢弃测试消息验证删除旧目标状态后不会重新消费旧 turn/cmd/sync；迟到设备处理有明确操作结果。不能以“库现在为空”代替重放验证。
- **性能与故障：** 在相同硬件/新建等价语料上比较旧实现基线与新实现，检查 p95/p99、锁等待、RSS 和错误率；继续遵守现有 voice 300ms 硬预算。沿用仓库已有 p95 回归超过 20%、R@5 下降超过 1 个百分点的判定，不新增完整评测平台。安全/遗忘断言零违规。

所谓“不兼容旧数据”只移除了生产迁移要求。为做 A/B 单独构建同样的测试语料不属于数据兼容；可以分别生成两份临时库。历史报告用于对照，不需把其全部 `3.8.0` 字符串替换成 3.9。

**七、测试补充、过期测试清理与交付门槛（本轮必做）**

测试与 P0–P4 同步实施。先修测试夹具，再给每个 O1–O4 改动配有区分度的行为用例，最后跑完整回归。测试数量不是验收目标：每个新增用例必须能抓住具体回归；已有用例充分覆盖的就扩展或复用，不再复制一份。缺口清单 G1–G5 仍不转为产品开发，但针对现有行为的测试不受此限制。

**7.1 已发现的测试问题，先处理夹具与错误证明。**

这次实际检查得到以下结论，不能继续依靠当前测试全绿推断升级安全：

- [test_restart_hygiene.py](/Users/manson/ai/eidolon/eidolon_memory/tests/memory/e2e/test_restart_hygiene.py:119) 的重启用例使用 `steward_mode="noop"`，写入 turn 不产生长期事实；最终只检查 recall 不报错。它还换成另一个 space，并向已经启动的 runner 的 palace 复制文件。这既没有证明事实持久化，也违反测试应遵循的文件单持有者操作方式，必须重写。
- [e2e/conftest.py](/Users/manson/ai/eidolon/eidolon_memory/tests/memory/e2e/conftest.py:182) 的 `_delete_e2e_durables` 手拼旧 consumer 名称，只清 turn/cmd，漏掉 sync；广泛吞异常。应复用正式 subject/consumer 命名函数，覆盖三类消息；只允许“资源不存在”作为正常清理结果，其他错误使夹具启动失败。
- 同一夹具的 `keep_palace=True` 保留 palace，但后面仍执行消息清理，不能用于证明未 ACK 消息重放。首次建库、同一状态重启、显式历史重置必须是三个不同模式。fresh 模式清 palace 和 sibling ledgers；restart 模式同时保留存储与 broker 状态；reset 模式调用受测的重置流程，而不是夹具预先帮它清好。
- `_wait_mcp_ready` 把任意非 5xx 响应当作就绪，404 也可能通过。改为真实 MCP initialize 和所需工具可用性检查；agent 与 ops 两个入口都验证。HTTP 可连接只作排障信息。
- 原 `test_mempalace_360_contract.py` 的内容已经是 3.8 契约；部分测试还只验证项目未使用的上游 daemon 或私有 repair 函数。应把现行依赖契约与历史调研探针分开。

每次 E2E 使用独立 broker/数据目录和端口、独立 space、独立模型 fixture；销毁时等待真实子进程退出。重置/删除场景必须使用该用例专属 broker，不能继承指向开发或生产 broker 的环境覆盖。普通重启与恢复测试不能在进程持有期间覆盖其文件。

**7.2 单元测试：补足输入边界、语义与失败路径。**

| 覆盖组 | 必须证明的行为 | 落点 |
|---|---|---|
| U1 类型化转换 | 中文/文本/JSON、空结果、同 room 多个 drawer、同 basename 不同来源；真实 ID 和 scope 元数据不丢；结果批次数与行对齐异常能被发现 | `test_backend_contract.py`、重组后的 adapter 转换测试 |
| U2 分数与排序 | 原始 similarity 不随 boost 改变；内部排序仍受 boost 影响；同分稳定；top_k 和边界距离处理明确；内部字段不泄漏到公开响应 | `test_mempalace_fast_search.py`、`test_recall_ranking.py`、`test_public_recall.py` |
| U3 查询统一 | 单 wing 与等价 scoped 请求一致；多 wing 一次 embedding；offline 只替换向量来源；voice 跳过 closets；候选合并后可见性不变 | `test_mempalace_fast_search.py`、`test_backend_contract.py` |
| U4 错误映射 | missing、数据库不可读、UnsupportedFilter、模型身份/维度错误、sanitize 拒绝各有确定结果；没有异常吞成空结果、放宽 scope 或绕过上游校验 | 当前 backend/config/init 测试 |
| U5 并发与关闭 | 读可并发、写互斥、等待中的 writer 不饥饿；取消前后两种任务状态；close 等待真实执行结束；重复 close、部分初始化失败；关闭 A 不关闭 B | `test_space_lock.py`、`test_locked_backend.py`、router 测试 |
| U6 权限与生命周期 | owner/companion/其他 space；设备相同/不同/未提供；归档和删除不可见；metadata 缺失处理确定；后过滤不能使正常可见候选被无效候选挤掉 | audience、recall policy、privacy 测试 |
| U7 新路径与重置 | 默认/配置/环境覆盖；只清目标 palace＋ledgers；旧重放入口处理；非法路径/越界链接被拒绝；其他 space、模型缓存、身份信息不受影响 | `test_palace_directory.py`、`test_history_reset.py` |
| U8 已有图与状态 | general 与新增统计字段的口径；空图和缺失集合区分；正常空结果与 degraded 区分；故障不显示为已保存 | owner graph、integrity、materialization/status 测试 |

以参数化覆盖有意义的组合，不对全部字段做机械笛卡尔积。并发测试用 event/barrier 控制执行顺序和状态，以有界超时作保护；避免依赖长 sleep 或执行快慢来“猜测锁正确”。优先断言外部行为或关系不变量，例如“相同向量距离的公开相似度相同”，不照抄生产公式生成期望值。

**7.3 真实 MemPalace 3.9＋Chroma 集成测试：不能只有 mock。**

至少包含以下场景，使用真实 `QueryResult/GetResult`、临时文件和最终锁文件安装出的包：

1. 空 palace 初始化、当前模型身份和维度写入；显式 document/query 向量查询；关闭后同一进程重新打开，真实 ID/metadata/正文仍在。
2. 用真实 `where` 执行 wing、audience、device 的组合筛选，确认支持的操作符和缺字段语义；错误分类用真正非法条件触发，而非只 mock 一个异常。
3. 同一进程两个 palace 的隔离、关闭一个后另一个正常工作；用可观察的重新打开、再次写入和持有者竞争检验句柄释放，不只断言 mock 的 close 被调用。
4. 更新、归档、批量删除和读回验证；分页、重复 ID 与空批次。3.9 新写入的 schema 和证据身份必须完整。
5. 初始化失败、模型维度冲突及数据库故障的准确报错；潜在 native crash/HNSW 故障在独立测试子进程或公开预检边界模拟，不能危及整个测试进程或访问真实 palace。

此层允许直接调用 adapter，因为验证对象就是 adapter 与真实存储契约；不得把它统计为完整服务 E2E。可使用确定性向量或本地受控 embedding endpoint 证明接线和排序，不能据此声称真实中文语义质量提高。

**7.4 端到端测试：覆盖完整服务与故障恢复。**

E2E 继续遵守现有约定：独立 runner 进程、真实 MemPalace/Chroma/SQLite、真实 NATS JetStream，通过 MCP 读和正式写入口操作。模型在确定性 fixture 或本地协议端点返回结果；不替换存储为 FakeBackend，也不直接调用 `process_turn_message` 后声称是 E2E。进程生命周期与停机重置操作由测试驱动，是明确的运维例外，普通业务读写不走数据库捷径。

| 场景 | 可验收的结果 | 复用/调整位置 |
|---|---|---|
| E1 首次使用 | 新库开始为空；通过 NATS 发一条能形成 assertion 的 turn；最终 MCP 召回包含确切事实与证据身份，状态从接收到真正完成 | model execution、canonical/隐私现有 fixture，必要时新建 fresh-store 用例 |
| E2 真正重启 | 先证明 drawer 和 KG 中有对应事实；停止 runner；相同 space、palace、ledgers 和 broker 状态重新启动；通过 MCP 再读到同一身份的事实 | **重写** `test_recall_survives_agent_restart`，不使用 noop＋空结果验证 |
| E3 重投与恢复 | 未完成消息在重启后继续处理；ACK 后重发同 source event 不生成重复事实；decision 持久化后的重投不重新执行模型抽取 | `test_operational_recovery.py`、restart fixture |
| E4 修改与遗忘 | 新库写入事实→修改/失效→归档/硬删除→重启→重放原事件；旧事实不重新出现，相关证据明文和状态符合现有契约 | `test_privacy_lifecycle.py`、canonical 路径 |
| E5 多角色与设备 | 同一事实由不同 companion/device 请求时符合权限；其他 space 的标记不会出现在正文、来源或统计中 | `test_multidevice_architecture.py`、现有隔离场景 |
| E6 关闭和交接 | 长查询/写入在途时发起停机；释放真实操作后新 runner 才能取得同 palace 所有权；其他 space 不受影响 | `test_concurrency_topology.py`、router lifecycle |
| E7 慢模型与故障状态 | 阻塞模型初始化/调用时已有记忆可召回；写入失败可观察；不能把读取失败或尚未投影显示成空记忆/成功 | `test_model_execution_isolation.py`、`test_cmd_not_starved_by_turns.py`、observability |
| E8 历史重置 | 构造旧 drawer/KG/ledger 与未完成 turn/cmd/sync；执行正式重置；新库为空、旧消息不自动重现；再写新事件可用，旁边未重置的 space 不受影响 | 扩展 history reset 的隔离 E2E；生产 canary 不替代此测试 |
| E9 真实接口协议 | agent/ops MCP 初始化、必需工具、鉴权、返回结构均可用；列表/单条读取/召回的同一事实身份和语义一致 | `test_mcp_surfaces.py`、HTTP/auth 与 E2E fixture |

E8 的迟到离线队列部分按第五节的操作方案验证：未清队列的生产者未恢复准入，完成切换后再开放；不以测试私自清完所有输入来冒充生产 reset 自己正确。未实现产品级代际拒收，就不能编写一个只靠 mock 返回拒绝而通过的“迟到事件永远被拒绝”用例。

**7.5 清理/替换/保留清单。**

| 对象 | 处理决定 | 替代或保留理由 |
|---|---|---|
| `test_mempalace_360_contract.py` 的名字和固定 3.8 断言 | 改为 `test_mempalace_public_contract.py`，验证最终依赖与当前 API 行为 | 上线版本断言必须参加测试，不能延续调研时的 deselect |
| 该文件的私有 repair helper、上游 QueueStore 及不使用的 sqlite_exact 正向行为测试 | 核对无生产调用后移出发布测试/删除；历史结论保留在文档 | 我们不依赖这些实现；由当前 sibling ledgers、NATS 幂等和 Chroma 行为测试承担实际责任 |
| `test_ledger_location.py` 中 4 个搬迁行为测试 | 随 `_adopt_ledgers_beside_the_palace` 删除 | 不再支持旧布局；保留 sibling 目录隔离和文件归属测试 |
| `test_adapters.py` 及其他旧 parser/dict 兼容用例 | 跟随 O1 调用审计改为类型化结果与现有外部响应契约测试 | 删除的是过时输入兼容，不是文本/JSON、scope 或 wire 行为保护 |
| 依赖 `_merge_tenant_queries`、重试同查询、异常返回空的旧测试 | 由失败可见、scope 不扩大、空库正常用例替换 | 不再把吞异常作为正确行为 |
| `test_restart_hygiene.py` 当前重启用例 | 必须重写，禁止向运行中的库覆盖文件 | 新用例检查相同 space 的真实事实、KG 和证据身份 |
| 该文件 source touch 导入测试 | 合并或重写；仅保留仍可复现且未被现有 lazy-import 保护覆盖的行为 | 修改隔离源码副本，不触碰正在协作的工作树；不以 noop 的 recall 成功当成 NATS 已消费 |
| `test_local_palace_router.py` 与 `test_router_contract.py` 的重复用例 | 合并相同场景；明确 fake 行为单测与真实存储集成测试各自职责 | 保留真实跨进程争用、初始化失败和关闭恢复等独有覆盖 |
| 字符串匹配私有函数体/文件内容的脆弱断言 | 行为测试可替代时删除或替换；层级和禁止依赖的架构测试保留 | 不因纯重构无效报警，也不失去真正架构约束 |
| 配置拒绝不支持 backend 的测试 | 保留 | 不支持 sqlite_exact 的约束仍有效，不能因不测其成功路径就删拒绝测试 |
| 隐私、audience、device、取消锁、模型隔离、快照恢复、NATS 幂等测试 | 保留并对新库重跑 | 旧生产数据可删，不等于当前版本无需持久化和恢复 |
| 历史基准报告及真实 LLM 测试 | 历史报告保留版本事实；真实 LLM 评测保留单独运行门槛 | 不能改历史数字/版本，也不能把离线接线测试冒充模型质量验证 |
| `scripts/benchmark/run_memory_perf_report.sh` | 核对调用者后修为现行配置或移除其现行入口引用 | 实查仍固定 sqlite_exact、旧三段 space ID、rules steward；不可作为本轮发布验收脚本 |

搬迁测试具体是 `test_an_existing_palace_migrates_on_open`、`test_migrating_twice_changes_nothing`、`test_a_stale_copy_left_behind_never_overwrites_the_live_one`、`test_a_real_sqlite_file_survives_the_move`。删除时同步去除 import，保留原文件仍适用的测试，不能整文件一刀切。

清理和替换必须跟对应实现修改同一阶段提交。不能先删一批测试让旧代码变绿；也不能用新增 skip、宽泛 xfail、降低断言或无限重试掩盖回归。交付报告列出“删除的旧要求→替代测试或不再支持的理由”。

**7.6 运行分层与最终报告。**

- 日常与合并前：运行当前单元测试、真实存储契约和确定性 E2E，以及 `contracts/tests` 中受影响的公开契约；默认命令明确排除 `llm` 与 `live_realm`。完成重构后跑一次完整适用回归，不只重跑上一轮 42 项。
- 检查 marker/路径收集结果，确保所有确定性 E2E 真正进入必测清单。源码放在 `e2e/` 不代表 `-m e2e` 一定选中；同样不能把真实 MemPalace 测试长期留在未开启的 live 环境变量后面。
- 发布验收环境必须备齐 MemPalace 3.9、Chroma、NATS 和本地模型 fixture。必测场景因缺依赖而 skip 视为验证未完成，不计作通过；本地缺可选资源的跳过与发布门槛分开报告。
- 真实模型质量与目标设备性能单独测量；确定性向量、模拟模型和开发机毫秒数不能替代它们。真实部署的 canary 只在明确目标上执行，不进入默认 CI，不能给通用测试继承生产配置。
- 报告至少包含 commit、依赖版本、命令、测试层次、passed/failed/skipped/deselected、删除/替换清单、日志和可复现场景；报告不带生产正文或凭据。
- 对新修复的核心回归，使用明确反例验证测试会抓住旧行为，例如去掉 close、把 boost 再混入 similarity、丢掉 query IDs、漏清 sync。做少量针对性反证，不引入完整 mutation-testing 平台。
- 必须做到 U1–U8、真实存储五类场景和 E1–E9 各有对应证据。允许一个有效用例覆盖多项，不设机械用例数量或全仓覆盖率目标；低价值重复单测不能替代真实链路。

额外 2–3 日优先花在夹具修复、真正重启/重投/重置及资源释放这几处；它们直接影响测试结论是否可信。新增测试不把 G1–G5 的未批准产品能力转为本轮交付。

**八、现有证据与待验证边界**

上一轮已下载并校验官方 3.9.0 wheel，SHA-256 为 `41ab339f72c5670280d8461b2837a22b0b25ef8a017dfebd31eabb1903101f30`。在当前依赖环境中通过临时 `PYTHONPATH` 加载该 wheel，现有六个相关测试文件结果为 **42 passed、1 deselected、1.07 秒**；排除的是明确要求运行时为 3.8.0 的断言。这是实施前的初步证据；当前 `.venv` 已是 MemPalace 3.9.0、ChromaDB 1.5.9。

这些只证明受测 API、快路径和锁契约没有发现兼容回归，不证明完整 3.9 锁定依赖、新库完整链路、实际设备性能或上面 O1–O4 已经通过。此次进一步完成的是源码调用链、公开签名、资源释放及重放范围分析，未把尚未实施的重构说成已测试。

上面的 42 项属于实施前调研，不能当作交付结果。当前实施与验收结果以第九节及验证记录为准。第 3 点继续按原文“有的话也不上”执行，不新增 G1–G5 的产品能力。


**九、实施记录（2026-09-14）**

代码基线为 `a250317`，本轮交付以本文件所在提交为准；未推送或发布。

| 阶段 | 当前结果 |
|---|---|
| P0 | `mempalace==3.9.0`、锁文件和本地安装一致；Chroma 保持 1.5.9，没有连带升级其余运行依赖；现行探针和契约测试使用中性名称 |
| P1 | 查询返回直接转换为 `MemoryWireRecord`，沿用真实 QueryResult/GetResult ID；保留来源和权限元数据；单 wing/scoped/offline 共用查询链路；公开 similarity 与内部排序分离；删除旧 payload parser 和类型兼容辅助函数 |
| P2 | `LockedBackend` 关闭先停止新操作，再等待实际操作完成；关闭调用方取消不取消收尾；Router 初始化取消仍保有结果、失败释放声明、关闭失败保留所有权并允许重试；通过公开 `close_palace` 关闭单宫殿；runner 在服务原事件循环内关闭 Router |
| P3（仓库） | 默认路径、当前 settings、example、README 和 CLI 帮助更新为 v3.9；删除旧 ledger 搬迁和不支持后端的死分支；正式重置脚本覆盖 sibling ledgers，并使用 space＋palace 两种锁；拒绝链接到其他位置的历史目录 |
| P4（验证） | 已运行完整回归及新增真实存储、重启、权限、重置、模型重投测试；最终命令、结果和日志见 `MEMPALACE_390_VALIDATION_2026-09-14.md` |
| 实际设备清库/发布 | 尚未执行。本机 `EIDOLON_STATE_ROOT` 未设置，默认 memory 根目录不存在；registry 配置指向外部 system-data 服务。已询问目标环境。不能把临时测试库重置说成实际环境已清空，也不猜测离线设备队列的位置 |

本轮执行中修正了三处原先会被兜底或测试掩盖的问题：

1. **房间名生成。** canonical assertion 和 theme 曾生成带冒号的 room，不满足上游名称校验；旧代码吞掉校验错误后继续写。现在生成符合契约的名称和稳定散列 token，拒绝非法输入，完整 NATS 写入链路已验证。
2. **关闭与重启。** 旧 runner 先结束 `asyncio.run(server.serve())`，再通过第二个事件循环关闭 Router；后台任务可能先被事件循环取消。现在在服务循环退出前完成关闭。NATS 工作任务退出后刷出最终 ACK 并关闭连接，避免等待已放弃的 pull 请求；重启测试明确要求正常退出码，不能靠 SIGKILL 过关。
3. **测试真实性。** 重启测试不再复制运行中的数据库，也不以 noop 空结果冒充持久化；夹具使用真实 MCP 初始化，同空间重启保留 palace、ledgers 和队列。`test_multidevice_architecture.py` 原来使用 FakeBackend，已移回普通集成测试目录；新增真实 MCP/NATS 的 scope 用例。重置测试实际调用正式 CLI，覆盖三类 subject 的清理和邻居空间不受影响。

**明确保留的边界：**

- 3.9 的公共 SQLite/BM25 降级 payload 不包含 audience/device 等自定义元数据；确认 HNSW 不安全时不打开向量集合。若 SQLite 返回非空候选，返回存储不可用，由现有接口报告 degraded，不能把权限不可核验伪装成正常空结果。没有新增全量 metadata 扫描、私有 SQL 回填或第二套搜索实现。
- `graph_stats` 中 general 房间和 room instances 的统计按 3.9 验证；没有新增 explicit tunnel 写入/导航。上游 tunnel 文件位于 palace 的父目录，多空间扩展仍属于 G3，不能把其路径当作每空间独立存储。
- 真实存储测试使用明确的确定性向量；模型端到端测试使用独立、受控 SDK 子进程。它们证明协议、权限、持久化、去重和故障行为，不证明新模型的中文语义质量或目标设备性能。
- 实际发布仍按第五节顺序处理生产者、turn/cmd/sync 和离线待发批次。用户允许删除旧数据，不等于允许清除上线后新产生的记忆。
