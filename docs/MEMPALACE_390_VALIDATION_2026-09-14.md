# MemPalace 3.9 升级验收记录

日期：2026-09-14。基线提交：`a250317`；本轮变更以本文件所在提交为准，没有推送或发布。

## 运行环境与结果

- macOS arm64，Python 3.13，MemPalace **3.9.0**，ChromaDB **1.5.9**，pytest **9.0.3**。
- 依赖通过 `uv lock`、`uv sync --extra dev --locked` 安装；运行依赖只更新 MemPalace，其他传递依赖保持原锁定版本；最终 `uv lock --check --offline` 通过（114 个包）。
- 完整适用集合：**1295 个测试**，另 11 个 `llm`/`live_realm` 用例默认排除；确定性 E2E 为 **25 个**，都进入默认集合。已将误放在 E2E 目录、实际使用 FakeBackend 的多设备测试移回普通测试目录。
- 最终全量结果：**1295 passed、0 failed、0 skipped、11 deselected，350.89 秒**。
- 前一次完整回归：**1292 passed、0 failed、0 skipped、11 deselected，315.95 秒**。后补的重启/去重/隐私用例分组 **5 passed**，存储相关分组 **30 passed**，查询边界分组 **13 passed**。
- 43 个改动 Python 文件的完整 Ruff 检查通过；全仓正确性规则 F821/F811/F841/F401 通过；`git diff --check` 通过。

最终验收命令：

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests contracts/tests -q -rs
```

默认 marker 明确排除真实远端模型评测和生产 Realm 运维 canary。未通过添加 skip/xfail 规避新版运行依赖测试；原来由环境变量门控的真实存储测试已换为必跑的临时 Chroma 契约测试。`PATH` 包含开发依赖目录，使现有 Ruff 测试门实际执行。

本地日志在 [reports/mempalace_390_upgrade_20260914](/Users/manson/ai/eidolon/eidolon_memory/reports/mempalace_390_upgrade_20260914)；该目录按仓库规则不纳入 Git，关键结果保存在本文。

## 验收覆盖

| 计划项 | 实际证据 |
|---|---|
| U1 / O1 | `test_mempalace_results`、`test_mempalace_fast_search`、`test_mempalace_storage`：文本/JSON、真实 ID、同 room 多 drawer、同 basename 不同路径、来源/权限/时间完整性、结果行不对齐报错 |
| U2 | `test_recall_ranking` 与 fast search：raw similarity 不随 boost 改变、内部排序受 boost 影响、稳定同分、内部字段不暴露、零结果不触碰存储 |
| U3 | 单 wing 与 scoped 真实查询结果一致；多 wing 只 embed 一次、voice 不读 closets；offline 仅替换向量来源；真实 where 组合 |
| U4 | 真实 UnsupportedFilterError、维度不匹配、非法名称拒绝；unsupported filter 不会去掉 scope 重试；SQLite 降级缺少权限 metadata 明确报不可用 |
| U5 / O3 | `test_locked_backend`、`test_space_lock`、router：取消后实际操作继续持锁，close 等待；关闭取消/失败可重试；初始化取消与失败、两种所有权锁、同进程多 palace、单 palace 关闭与重开 |
| U6 | 现有 privacy/audience/recall policy 用例，加真实 Chroma wing/audience/device 查询；E2E companion、设备相同/不同/未提供、不同空间的隔离 |
| U7 / P3 | `test_palace_directory`、`test_history_reset`：v3.9 默认/覆盖配置、palace＋sibling ledgers 清理、目录链接拒绝、跨名称同路径持有者拒绝、邻居空间保留 |
| U8 | 真实 general 房间与 room instances 统计；现有 graph、integrity、状态和 degraded 用例继续跑；未新增 tunnel 导航 |
| 五类真实存储场景 | `test_mempalace_storage`、`test_local_palace_router`、现有 backend privacy 与 init/integrity/restore 测试，直接使用锁文件安装的 MemPalace/Chroma，涵盖空库、类型化结果、过滤、归档/删除、维度/完整性、关闭/重开/争用 |
| E1 / E7 | `test_model_execution_isolation`：受控 SDK 运行在真实模型子进程，NATS turn→写入→MCP 召回；阻塞模型期间 MCP 与发现接口可用；既有故障/状态用例保留 |
| E2 | `test_restart_hygiene`：MCP 证明 drawer 和 KG 有事实，正常退出，相同空间/目录重启，原 ID 和 command ledger 仍在；停机时发布的命令继续处理 |
| E3 | 模型重投用例：重启后再次发布相同 turn，确认 broker 出现新 delivery 且已 ACK，再检查只有原 storage ID、SDK 调用次数不增加；现有并发重投、DLQ replay/resolve 保留 |
| E4 | `test_privacy_lifecycle`：归档/硬删除、确认流程、原事实不能重新激活；重启后再次重放仍失败，召回无已删除事实 |
| E5 | `test_scope_visibility` 与现有不同 Realm 进程测试：真实 MCP/NATS、多 companion、多 device、其他 Realm 正文不泄漏 |
| E6 | `test_concurrency_topology` 的跨进程争用，router/锁测试的在途操作释放顺序，同空间正常退出码＋重启后的真实存储读写；没有把直接复制运行中的数据库作为证据 |
| E8 | `e2e/test_history_reset` 实际运行正式 reset CLI；存储中有 drawer/KG/ledger，broker 中有待处理 turn/cmd/sync；重置后库和对应 subjects 为空，再写新事实正常，邻居 Realm 与注册身份不变 |
| E9 | 每次启动都对 agent/ops 的正确路由做真实 MCP initialize；现有工具集合、HTTP/auth、响应结构契约继续参加完整回归 |

E8 的 sync 待处理项使用明确标记的测试消息验证清理范围，turn/cmd 使用正常消息封装。该测试不宣称设备上的离线队列已清，也不宣称未增加的代际拒收协议存在。

## 清理及替代

- 删除 `search_payload.py` 和 dict/typed 双形态兼容辅助函数；旧 parser 测试由类型化结果/公开响应测试替代。
- 删除 `_adopt_ledgers_beside_the_palace` 及四个旧布局迁移用例；保留 sibling ledgers 的布局与归属保护。
- 当前上游契约从 `test_mempalace_360_contract.py` 改为 `test_mempalace_public_contract.py`；删除未使用上游 daemon/private repair/sqlite_exact 的正向实现测试。拒绝不支持 backend 的约束保留。
- 合并重复的 router 场景；保留真实存储、初始化失败、跨名称同路径锁等独有用例。
- 删除用 source touch 和 noop 空结果验证“重启成功”的旧 E2E；用同空间事实、KG、ledger、未消费命令与真实正常退出来替代。
- 删除已不能运行的 `run_memory_perf_report.sh`，其 sqlite_exact/rules/旧身份参数不再受支持；更新 benchmark README 指向当前测试和探针。历史报告中的原始数字/版本保留。

## 针对性反例

未改动工作树源码，在独立 Python 测试进程中注入三个旧行为：把内部 boost 写入公开 similarity、丢弃实际 storage ID、close 不等待操作。三个反例均导致对应新回归测试按预期失败，证明断言可抓住这些错误。日志 `counterexamples-expected-failures.log` 是故意注入缺陷的结果，不能混入正常测试失败数。

## 上线范围

仓库代码、依赖、配置、重置工具与适用测试已经纳入本轮工作。**实际环境清库、设备离线队列处理、部署和设备性能验收尚未执行**：当前本机状态根目录未配置，默认 memory 目录不存在，尚无确定目标主机/部署目录。已向用户询问；不使用临时环境结果冒充上线结果。

3.9 的 SQLite 降级搜索缺少权限元数据，因此非空候选会进入现有 degraded 路径。没有添加 metadata 全库扫描、私有 API patch、BM25 union、closet 级联、新 UI 或事件流。确定性向量与受控模型只能证明接线和行为，不能证明中文语义质量提升。
