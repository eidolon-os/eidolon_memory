# Mac 个人记忆部署记录：2026-10-28

本次在现有 Mac Host 更新并重启项目服务，没有创建新环境或重启 macOS。Android APK 已覆盖安装到唯一在线的 PHK110 手机，保留现有应用数据，并成功启动应用进程。

## 固定来源

发布前发现线上已有短语音识别、长 ASR 尾部过期降级、识别诊断和记忆提示修复。将其与本轮个人记忆实现合并后再发布，未回退线上能力。Models 仍为 `7823c49d9597fb234e6ce6997ac47bd92516448f`，识别等待预算仍为 400 ms。

| 仓库 | 部署/构建提交 |
|---|---|
| eidolon_sdk | `bc2af3e1bad33fbb1337a2c0ab61952475e96d08` |
| eidolon_data | `d21e9946183566ecd006623266a0bcac281001aa` |
| eidolon_admin | `a28cca8faf64534f9108cc0ff6d5211a386cc168` |
| eidolon_agent | `7b5840e7dc462431dae0d76c3d70ec3dac36f89e` |
| eidolon_channel | `a5617ea126585d73d5adf573b364585ef98365c9` |
| eidolon_memory | `0f4ce744a9d522565cd4f3d201d75c4be396e466` |
| eidolon_client_mobile | `78ef04763dd5a95a92c189c648e9f8b21dc899e2` |

## 验证

- 合并回归：SDK 63 passed、Agent 273 passed、Channel 146 passed。
- Ops start / status：healthy，所有健康检查通过。
- Ops app-ready：app_ready，17 项检查全部为 true，包括 worker 身份、LAN、LiveKit 和管理接口。
- 运行环境实际导入固定源码 SDK，支持 source_session_id；Agent TurnContext 与记忆状态提示参数可用。
- Android 覆盖安装返回 Success，重启应用后进程存在。未自动加入房间或启用录音。
- APK SHA-256：`bd0ae795cf732b4369395ad887bfbef6546d90b40102027deff8e7be4cb52a23`。

## 备份与验收范围

更新前通过 Ops 保存各数据库与记忆空间快照，目录 `/private/tmp/eidolon-before-memory-20261028`，同时保存原 Host 配置及目标版本。各 authority 分别快照，不是整个 Host 的一致时间点镜像；此次没有重置数据或调整数据库 schema。

本次完成部署与启动验证，尚不代表真实多人声学准确率、A/B/unknown 连续对话、个人记忆检索写入及在途撤权均已完成真人验收。后续在 Mobile 人物管理中明确启用目标 Person 的记忆，再进入房间验证。无有效识别或未启用记忆时，正常对话且不使用个人长期记忆。

## 后续更新：Person 生物特征档案

同日完成声纹一人一档与人脸参考照片管理后，Host 再次升级并通过 17 项 app-ready 检查。新版本、迁移和测试详情见 [Person 声纹与人脸档案](PERSON_BIOMETRIC_PROFILE_DESIGN.md)。本次新增 APK 已构建，但手机离线，安装未完成；上文的安装成功仅指此前个人记忆版本。
