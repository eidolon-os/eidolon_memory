# 声纹会话与 Memory 的集成交接

更新时间：2026-10-28。此文件记录另一识别会话的实际集成状态，不替代本仓 `MULTI_PERSON_MEMORY_ISOLATION_DESIGN.md` 的 Memory 设计。

完整链路、契约、测试和限制的唯一说明在 [OS 用户与声纹辅助识别重构](../../docs/跨系统/OS用户与声纹辅助识别重构.md) 的“对话识别实施与 Memory 对接”。识别工作区位于 `../.eidolon/worktrees/conversation-person-20261027/`，各仓分支 `codex/conversation-person`。

本侧已完成但尚未部署的 v7 改动，经三方合并进入该独立集成分支，并在 Mac 联合部署；未修改本会话主工作区代码。SDK `2f12aae`、Data `3cfe41f`、Channel `201b7e6`、Agent `922892f`；Mac Memory 继续使用 `470eecb`。当前“无 Person 不读写长期记忆、按 session 隔离历史、个人凭据绑定 person_revision”已经运行，不能继续按旧 v6 状态判断。验证来源摘要及实际服务证据位于 `../.eidolon/person-recognition-validation-20261027/`。

识别侧输出复用 SDK `SpeakerIdentityObservation`。完整范围、Owner/session、Channel turn/跨服务 trace、Person revision、模型和策略版本都有绑定；Agent 入站验证专用短期签名后放到 `TurnContext.speaker_observation`。授权 `TurnContext.person_id/person_revision/identity_epoch` 不变。当前模型策略仅 controlled_trial，不能自动开启私人记忆；Mobile 无手动 Person 选择器。

尚待联合完成：正式验证识别策略后，由可信会话编排核验 Data active Person、观察 revision、明确启用且 ready 的 Realm；换人/unknown 时完成旧生成取消、Body 播放清理确认和新 session/conversation，再签发 v7。观察不能创建/恢复 Realm；无音频输入不能继承最近人物。管理端记忆启用/恢复入口、进行中的撤权关闭、真人 A/B/unknown 验收仍未完成。

已实测有效“无音频”观察接受、伪造观察忽略、普通回复均成功，授权 Person 为空且长期 Memory outbox 为零。这不是自动私人记忆端到端验收。
