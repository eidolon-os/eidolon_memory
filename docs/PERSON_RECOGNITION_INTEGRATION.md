# 声纹会话与 Memory 的集成交接

更新时间：2026-10-28。此文件记录另一识别会话的实际集成状态，不替代本仓 `MULTI_PERSON_MEMORY_ISOLATION_DESIGN.md` 的 Memory 设计。

完整链路、契约、测试和限制的唯一说明在 [OS 用户与声纹辅助识别重构](../../docs/跨系统/OS用户与声纹辅助识别重构.md) 的“对话识别实施与 Memory 对接”。识别工作区位于 `../.eidolon/worktrees/conversation-person-20261027/`，各仓分支 `codex/conversation-person`。

本侧已完成但尚未部署的 v7 改动，经三方合并进入该独立集成分支，并在 Mac 联合部署；未修改本会话主工作区代码。SDK `2f12aae`、Data `3cfe41f`、Channel `e8ae4ca`、Agent `922892f`；Mac Memory 继续使用 `470eecb`。当前“无 Person 不读写长期记忆、按 session 隔离历史、个人凭据绑定 person_revision”已经运行，不能继续按旧 v6 状态判断。验证来源摘要及实际服务证据位于 `../.eidolon/person-recognition-validation-20261027/`。

识别侧输出复用 SDK `SpeakerIdentityObservation`。完整范围、Owner/session、Channel turn/跨服务 trace、Person revision、模型和策略版本都有绑定；Agent 入站验证专用短期签名后放到 `TurnContext.speaker_observation`。授权 `TurnContext.person_id/person_revision/identity_epoch` 不变。当前模型策略仅 controlled_trial，不能自动开启私人记忆；Mobile 无手动 Person 选择器。

尚待联合完成：正式验证识别策略后，由可信会话编排核验 Data active Person、观察 revision、明确启用且 ready 的 Realm；换人/unknown 时完成旧生成取消、Body 播放清理确认和新 session/conversation，再签发 v7。观察不能创建/恢复 Realm；无音频输入不能继承最近人物。管理端记忆启用/恢复入口、进行中的撤权关闭、真人 A/B/unknown 验收仍未完成。

已实测有效“无音频”观察接受、伪造观察忽略、普通回复均成功，授权 Person 为空且长期 Memory outbox 为零。这不是自动私人记忆端到端验收。

Mac 当前等待上限为 400 ms；已录入样本三次本机回放匹配，327–348 ms。Channel 在完整最终 ASR 证据到达时提前开始，与 EOT 收尾重叠；这不构成持出样本声学验收。详细证据见上游唯一链路说明。

## 短句与反馈修复交接（2026-10-28）

Mac 已升级 SDK `9dc18a5`、Channel `94e6b28`、Agent `344418c`；Data / Memory 保持原版本，app-ready 通过。`SpeakerIdentityObservation` 新增可选 `audio_quality`，不改变 person_id / revision / session / 签名绑定，也不改变授权 `TurnContext.person_id`。Models v2 允许有效语音至少 600 ms 的会话探针，录入仍至少 1500 ms；短于 1500 ms 且匹配不足的结果为 uncertain/short_speech_inconclusive，不能当作 unknown 或沿用上一轮 Person。

Agent 的解释与 Mobile 状态已区分质量不足、短探针不确定、完整 no_match、服务失败。Agent 使用真实 memory_enabled 约束记忆承诺：false 时只能在当前对话使用自报姓名，不能承诺下次记得。已按第 26 节同步去掉试验标签对 UI 和回答的影响，标签仅作诊断。当前运行基线尚未合入该节的自动记忆编排；后续按识别状态及 Data 授权办理，不再增加 validated-only 门槛。公开对话连续保留，私人检索按轮隔离；本轮不新增平行身份缓存。联合合并须同时携带 SDK 的 audio_quality 字段，避免旧版严格契约拒绝新观察。

真实 422 的具体错误码及音频质量指标现可追踪，旧会话已经丢弃的错误细节不能恢复。部署验证记录在 `../.eidolon/person-short-speech-validation-20261028/`，完整设计仍以上游唯一链路说明为准。
