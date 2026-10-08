# MemPalace 词法 metadata 解码对照 — 2026-10-08

## 结论与边界

已取得锁定版本 v3.10.0 的上游源码，提交 `22fd87f09c19d5ffb2d6966486483353937931c0`。在上游 `ChromaCollection._lexical_search_via_sqlite` 中，复用同文件 `sqlite_list_id_metadata` 已有的做法：一次解析列位置，逐行直接取值，省去每个 metadata 单元反复构建字典与扫描 `Row.keys()`。

这是**上游候选实验，未接入生产依赖**。Eidolon 的锁文件、已安装 MemPalace 与读取适配层均未修改。`upstream.patch` 保留可审查差异；没有新增 SQL 过滤编译器、业务缓存、候选上限或另一条检索路径。

## 实验

`probe.py` 在临时 SQLite 中构造 10,000 条合成文档，每条 17 项 metadata；9,000 条错误 wing 排在 1,000 条 target wing 前。比较原方法与修改后的完整返回对象，包括公共 ID、正文、metadata、分数和顺序。

- 8 种过滤条件 × FTS 命中/短词回退/无命中 3 种查询 × 4 种 schema，共 **96 对输出完全一致**。
- 覆盖字符串/整数/浮点/布尔、缺失字段、AND/OR、in/nin/contains，以及逐步移除 bool/float/int 列的旧 schema。
- 上游现有两项 lexical 测试通过，含真实 Chroma 公共 ID 回读。
- 同库新旧交替顺序，各 12 次；全部逐次耗时保留在 `results.json`。

| 过滤范围 | 原实现中位数 | 候选中位数 |
|---|---:|---:|
| 两个 wing，共 10,000 条 | 277.5ms | 195.9ms |
| target wing，1,000 条 | 249.3ms | 162.8ms |

这不是原 1k/5k/10k MCP 矩阵，不能拿这些数与其 p95 相减，也不能宣布 voice SLO 达标。实验期间机器另有隔离模型抽取进程，非独占基准；交替对照仅支持继续验证该候选。全候选 metadata 仍然被物化，scope 过滤下推尚未实现。

## 复查

在同一上游 tag checkout 上应用 `upstream.patch`，修改 `probe.py` 的 SOURCE 指向该目录，以 Memory 的锁定虚拟环境执行脚本即可。脚本拒绝输出不一致；不会修改现有 Palace。

下一步应通过上游依赖正常发布/固定版本接入，再复跑原 MCP 矩阵。不能通过修改 site-packages 或在 Eidolon 中 monkeypatch 来声称已完成 T1。
