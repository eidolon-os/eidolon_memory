# Mac Host 抽取协议半更新事故（2026-10-08）

## 影响与根因

小禾对话进入 Memory，但没有可浏览事实和图谱。调查时有 21 轮记录、20 个抽取决策，其中 3 个 should_write=true，却没有 fragments/triples/intents。Realm 的图谱为空，仅有的 2 条旧文字事实属于另一伙伴。Mobile 的 companion_id 正确，相关管理接口返回 200；空数据不是 UI 缓存的结论。

Memory Realm worker 于 09:43 启动；11:07 的 84de82a 将提示词输出从 fragments/triples 改为 claims，并增加配套解析逻辑。常驻 Python 没有重新加载，但 render_steward_prompt 每次调用都读磁盘。旧解析器忽略未知顶层字段，因而吞掉新版 claims，同时将空决策作为成功持久化并 ACK。旧 extraction_version 仅跟随提示词等配置，不包括解析协议，重启后即使重投也可能命中这个空决策。

这是抽取策略生命周期和校验边界的设计缺陷，加上交付时未更新常驻服务、未验收真实写入的工程遗漏。静态单测在同一 checkout 同时加载新提示词和新代码，无法证明旧常驻进程已更新。源码运行不是自动热更新；已有 Ops 正式 restart 路径能够更新，不需要另外建设 watcher 或进程控制器。

## 修复

Memory main 7336ee5：

- 初始化时深拷贝配置并固定渲染后的系统提示词；整个进程内同一个策略对象使用同一版本，不再热读一半协议。
- extraction_version 包含显式解析协议 atomic-claims-v2、claim/decision schema、模型与语义配置，并在初始化时固定；启动日志输出实际加载版本。
- 模型输出必须是对象；未知顶层字段和没有持久动作的 should_write=true 均产生 StewardOutputError，走现有 NAK/DLQ，不能保存空成功决策。历史账本模型仍可读旧记录；拒绝只放在不可信模型输出边界。
- 保留旧 fragments/triples 解析兼容；禁止混合新旧格式和模型伪造内部 unified_claims 标记。

## 实际更新与恢复

通过现有入口 `eidolon_ops/.venv/bin/eidolon-ops --config config/hosts/mac.toml service restart memory-supervisor` 更新 supervisor 及 Realm worker。eidolond 审计位置 433，状态返回 ready；worker 实际加载 `atomic-claims-v2` / `llm:73ceb36e927aea14`。未重启其他组件。

只选择小禾的 3 个已保存为空且 should_write=true 的受影响事件，从现有 JetStream 读取原始字节，核对来源序号/哈希、原始 input_hash、Realm、companion_id 和删除标记后，通过既有 JetStreamCommandPublisher.replay_raw 重投。没有直接修改事实数据库，没有新建 writer，没有删除原决策。

新版本重新抽取后：2 轮产生事实及图谱投影，1 轮明确拒绝省略主语且无上下文的片段。该轮“是索尔的小狗”不能独立确定主体；不把它编造成铁锤的事实。跨轮上下文仍为独立未解决项，本事故修复不冒充已解决它。

Mobile 使用的管理后端 `/api/internal/v1/management/memory/library` / `graph`，按原 Owner 和小禾 companion_id 鉴权读取：

| 时点 | 记忆条目 | 图谱节点 | 图谱关系 |
| --- | ---: | ---: | ---: |
| 修复前 | 0 | 0 | 0 |
| 重处理后 | 2 | 3 | 2 |

这是实际后台 API 验证，没有声称操作了手机 UI。再重投完全相同的 3 个事件，日志出现 3 次 decision_reused；仍为 2 条事实，未重复调用抽取生成新决策。每个原事件保留新旧两个版本，旧空决策未抹掉。另一伙伴的 2 条旧事实仍被小禾视图隔离（withheld_count=2）。

## 验证与后续交付要求

完整测试初跑：1343 passed、4 skipped、11 deselected；29 个涉及本机端口的测试受沙箱限制失败/报错。获得本机网络执行能力后这 29 项全部通过（285.62 秒），不是跳过。随后抽取/恢复专项 15 项通过，覆盖提示词在运行中被编辑、新旧协议身份、空写入 NAK 且不污染账本、旧空决策恢复、同版本不重复抽取、跨版本仍尊重删除 tombstone。Ruff 和 diff 检查通过。

今后凡修改抽取协议或提示词，交付必须包括：兼容性/运行中修改回归、正式重启、核对实际 worker 的策略版本、真实事件决策与投影、管理后端可见性。代码提交或服务 health 为绿不是版本更新证明。提示词、schema、模型配置由版本哈希覆盖；解析/证据语义变化须同时升级 EXTRACTION_PROTOCOL。失败数据恢复必须用原事件和正常消费链，不清库、不直接补事实，不绕过删除与可见性规则。

本次私有原事件及重放清单只保留于操作者私有临时目录，不将用户对话、凭据或原始模型响应入库。
