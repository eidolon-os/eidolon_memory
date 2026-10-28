# 个人记忆会话绑定：安全协议变更评审

更新：2026-10-28。当前已部署基线为 v7；本轮工作区优化采用“连续房间会话 + 按轮个人记忆”，尚未联合发布。以 [隔离方案第 26 节](MULTI_PERSON_MEMORY_ISOLATION_DESIGN.md#26-连续房间对话与按轮个人记忆2026-10-28) 为准。以下保留原协议评审历史，其中“换人必须新建会话”不再用于新的自动识别房间链路。

## 核心目标与范围

只完成已授权用户的个人 Realm 读写。复用共享 Runtime JWT、Data 目录、Agent 会话授权和现有 HistoryFanout/outbox。首版切换人物必须开启新会话，不做同一运行中会话的热切换。不接自动声纹授权、家庭共享或多模态。

## 拟修改的协议

| 项目 | 既有行为 | 拟定行为 |
|---|---|---|
| 无个人绑定 | v5，Owner/Companion/Device/Session | 统一 v6，不携带个人字段 |
| 显式个人模式 | 不支持 | v6，在同一已签名 payload 中加入 person_id、identity_epoch |
| person_id | 无 | 稳定非空字符串，长度 <=64，不含首尾空白 |
| identity_epoch | 无 | 严格非负整数，拒绝 bool、字符串和缺失 |
| 字段组合 | 无 | v6 个人模式必须两字段齐全；默认模式两字段均省略，显式 null 拒绝；v5 及未知版本拒绝 |
| 加密与撤销 | 既有 HMAC、exp/iat/jti 和撤销检查 | 保持，不新增密钥系统、不放宽算法 |
| Realm 选择 | Data 默认 Companion snapshot | 个人模式通过 Data 的 owner/person 查询解析，不接受客户端提供 realm |
| 个人查询失败 | 尚无路径 | 返回明确失败，禁止降级到 Owner 默认记忆 |

person_id 是已授权绑定，不是 SpeakerIdentityObservation。签发方必须在现有已认证管理/会话流程中核对 Owner 管理范围、Person active 和个人 Realm active；不得直接信任 LiveKit participant metadata 或模型给出的 person_id。

## 收发端接线顺序

1. SDK 统一 v6 签发/验证与负例测试，删除 v5 兼容路径。
2. Agent 从已验证 RuntimeIdentity 读取绑定，交给 RuntimeSessionAuthorizer。Data 查询每次确认 person/realm 状态；不以长期缓存绕过停用。
3. AuthorizedRuntimeSession 冻结个人绑定和已解析 Realm，TurnContext、召回、承诺查询、HistoryFanout/outbox、失败重试全部继承同一范围。
4. 个人召回必须返回匹配的 identity_binding；缺失、错 Realm、错 person、错 epoch 都丢弃。无绑定响应兼容只允许旧无个人模式。
5. Channel 的显式个人选择在可信会话建立处接入。切换人物关闭旧会话并取消生成、排队播放及注入上下文，新建会话并签发新凭据。禁止仅替换当前 Realm 字段。
6. 接收方与写入方验证完成后才启用签发。协调升级 SDK、Agent 和 Channel；旧验证者拒绝 v6，新验证者拒绝 v5。已有连接需重新签发并建立会话，不提供自动降级。

## 发现的历史隔离风险

Agent `infra/persistence/agent_runtime.py` 的历史 hydrate 按 conversation_id 读取；因此只增加 person claim 或更换 session_id 还不足够。个人模式必须将 conversation 与不可变的 Owner/person/Realm/session 绑定，拒绝另一范围复用该 conversation_id。该检查必须发生在历史加载前；不能依赖 Memory 返回的过滤结果清除已加载的历史。

第一阶段使用新会话与新 conversation，并在持久层检查不一致绑定。旧未绑定 conversation 不自动认领为某个人，避免把混合历史交给新人物。真实数据库迁移与历史归属不在自动升级时推断。

## 必须通过的测试用例

- 默认和个人模式统一 v6；v5 无论是否带 person 都拒绝；过期/撤销行为保持。
- v6 合法绑定签发后验证一致；person 空值、缺失 epoch、负数、bool、字符串全部拒绝。
- v6 默认模式可无个人字段；显式 null、缺少配对字段、未知版本、签名篡改、跨 Owner/person 目录结果全部拒绝。
- Data 404/409/超时不触发默认 Realm 查询。
- 已停用 Person/Realm 的新请求被拒绝，不从缓存继续服务。
- A/B 使用相同设备和 Companion，召回与异步写入仍按各自 Realm 分离。
- B 复用 A 的 conversation_id，在读取任何历史前拒绝。
- A 的迟到响应/排队音频不进入 B 新会话；已接受写入保持 A 归属。
- 个人模式连接旧 Memory（无 identity_binding）时拒绝使用其结果。
- outbox 重试重启保留 person/epoch/Realm，不读取“当前人物”重写归属。

## 审批边界

自动审批拒绝直接修改共享 runtime token 签发与验证语义，理由是新增凭据版本和身份字段属于跨仓安全协议变更，需具体设计、测试及明确批准。请求批准实施本文件的 v6 契约与配套跨仓验证；不包含生产部署、生产数据迁移或自动声纹授权。

## 复杂度取舍（本轮结论）

v5 兼容没有已确认的分批发布需求，删除双协议分支。默认 Owner Realm 是保留的业务能力，不等于保留旧凭据版本。统一 v6 的代价是协调升级和旧凭据重新签发，应在发布时明确执行，不将失败降级成默认 Realm。

保留 SDK 已有的 Owner/Device/Session/JTI 撤销检查，它与版本兼容无关，也不新增撤销服务。撤销检查在验证凭据时执行，并不自动证明进行中的流、生成和音频已经停止；后者仍需要 Channel 会话生命周期完成关闭与清理。identity_epoch 是绑定的一部分，不单独构成在线撤销系统。

Agent 已在 TurnEngine 进入处理前注入持久 conversation guard，所有正常运行入口都经 bootstrap 接线。复用 ConversationRow 的现有 metadata 保存绑定和唯一主键仲裁并发认领；历史缓存、摘要和 LLM 均在检查之后使用。落盘再次检查相同归属；跨 conversation 重用已有 turn_id 也拒绝。测试/无持久化构造若缺 guard，个人模式直接拒绝。该检查不能替代 Channel 的可信身份选择，个人 gRPC 入口仍关闭。

## 最新实施边界

Channel 已接入可信 Provider dispatch 的个人选择、Data 查询、v6 签发及按 session 划分的 conversation。Agent 个人流的逐 turn 凭据/目录重新授权已作为候选路径验证。正常个人 gRPC 入口继续拒绝，原因已收敛为 worker 自动删除 dispatch 会绕过 Provider 的设备清理确认，必须统一关闭生命周期后再解除。详见设计文档第 19 节。测试中开放候选路径不等于产品入口已开放。

## 当前状态（第 20 节实施后）

Provider 已持久保存私人会话的关闭义务，dispatch 删除、worker 退出与重启不会释放未确认清理的设备；因此个人 Chat 的临时总阻断已移除。每 turn 重新授权、会话 guard、召回绑定和 outbox 归属均保留。个人 proactive、委派任务、任意事件发布及 Owner emotion 广播未开放。当前仅本地实现及测试通过，未部署；完整接口链路、错误码与待做真实设备验收见设计文档第 20 节。
