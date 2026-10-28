# Person 跨系统适配复盘

2026-10-28。由默认记忆页 409 问题继续追查。本记录保留最初审计发现；四项修复已实现，自动化验证见下文。部署结果另行记录，不能将代码测试等同于现网验收。

## P1：保留数据未进入完整备份目录

Data 的 memory-runtime-roster 仅发布 active Realm。Memory `UserAdminService.list_users`、`snapshot_user` 依赖该运行清单，Ops `hostagent/memory_realms.py` 的 capture 又仅遍历它返回的 Realm。暂停个人记忆或删除 Person 后，磁盘记忆仍保留，却退出这条备份枚举链；单独按已知 ID snapshot 也会因 registry 查不到而拒绝。备份可能看起来成功，但不含该保留空间。

修复方向：区分“持有数据的完整 Realm 目录”和“允许运行的 Realm 清单”。备份、恢复、保留数据清理使用完整目录，运行及会话授权继续只使用允许访问的集合。不能为备份而恢复已暂停人物的访问权限。

## P1：个人记忆治理入口缺失

Admin 的记忆 browse、search、export、forget 共用默认 Owner Realm 解析；Mobile 用户管理只有个人记忆开关，尚无指定 Person 的记忆管理入口。当前“你的记忆”只代表 Owner 默认空间，不代表全部个人空间。个人记忆能保存与使用，却不能通过相应产品入口查看、导出或遗忘。

修复方向：管理上下文显式标识默认空间或具体 Person，由后端验证 Owner/Person/Realm 归属；不能依赖最后说话人，更不能自动汇总所有私人空间。管理端查看归属不等于 mobile/device 在对话中主动选择说话人。

## P2：人物删除后的保留数据缺少后续操作

Persons.delete 停用私人 Realm 后删除 Person；保留 person_id 防止私人记忆变成共享数据。重新创建同 ID 被拒绝，现有恢复接口又要求 Person 存在。界面已明确历史记忆保留，但缺少保留数据的恢复/清理闭环。

修复方向：明确人物软删除和恢复，或提供独立的保留数据管理操作；在决定恢复语义前不擅自复用身份，不静默删除历史数据。

## P2：运行面板显示错误的当前 Realm

Admin mission_control/service.py `_memory_summary` 取 Owner 下第一个 enabled Realm，甚至回退 realms[0] 作为 active_realm_id。多个 Realm 共存时，此值既未必是默认空间，也未必是当前对话使用的空间。Memory supervisor 的 spec 也未传出已有的 scope。

修复方向：分别表示默认 Realm、个人 Realm 列表、最近一轮实际使用的 Realm；没有当前轮证据时不猜“当前空间”。这是状态展示错误，尚未发现这段值参与记忆读取授权。

## 已检查的正确边界

- 默认 Companion Realm 查询显式限定 person_id 为空。
- 人物停用与删除会停用私人 Realm；同名不合并身份；删除后旧 ID 不会自动接回保留记忆。
- Agent 逐轮核验 Person/版本/Realm，unknown 不启用个人记忆。
- device_requested 不允许显式选择 personal_memory。
- 交互会话禁用 Owner 级后台任务转交及事件工具，避免从个人上下文进入未适配的共享通道。
- Owner 删除收集其所有 Realm ID，不只收集默认 Realm。

定向回归：Data 人物、Realm、人脸和人物 API 16 passed；Agent runtime session、Data authority、gRPC generation 40 passed。这些不等于覆盖所有真人连续对话和在途异步写入时序；本次不宣称不存在其他缺陷。

建议顺序：完整数据目录与备份 → 个人记忆管理/删除闭环 → 运行面板 → 将“默认+两位 Person、暂停、删除、恢复”纳入跨服务业务验收。


## 实现闭环（2026-10-28）

1. **目录与授权分开**：Data 新增 `memory-realm-catalog`，保留所有 Realm，并明确 runtime enabled、management_enabled、person_id、scope。原 runtime-roster 保持运行期语义。Memory 的备份枚举使用完整目录；暂停不会因此恢复对话权限。缺少可验证 embedder 的空间会明确使备份失败，不冒充完整备份。
2. **默认与个人管理入口分开**：默认记忆不聚合私人空间；用户列表可打开指定人的记忆，查看、查询、导出和遗忘全程传递 person_id。后端以已认证 Owner 匹配该 Person 的 Realm，遗忘确认令牌绑定目标空间。
3. **暂停仍可治理**：暂停/归档空间运行 management-only worker。Owner HTTP 接口保留认证，MCP、对话订阅、设备同步订阅和 consolidator 关闭；命令 worker 仅接受 admin 的明确删除命令。
4. **人物归档与恢复**：Data 0015 增加 deleted 标记。删除清除声纹、人脸档案并暂停记忆，人物保留在“已删除用户”。恢复仍为 inactive，记忆仍为 inactive，不能恢复已删除的生物识别证据；启用用户和记忆仍需明确操作。
5. **清空保留记忆**：只对已归档 Person 开放，并由 Data 持久化 pending 清空意图。Memory 校验 Owner、Person、Realm、停用状态和明确的 erasure_pending；停止该空间进程，持有维护锁，清理该空间队列、消费者、Palace、账本及修复副本。完成后 Data 确认 completed。失败保留 pending，阻止恢复；重试复用意图。此操作不删除历史对话或旧的独立备份，界面确认说明这一范围。
6. **面板不猜归属**：分别展示默认空间、个人空间及最近一轮明确使用的空间。未识别/未使用记忆/无证据时为空，不再取列表第一个。
7. **维护竞争保护**：清空和恢复期间，周期刷新、初始化完成回调和子进程重启均不能重新启动该 Realm。停止失败不进入清理，退出时移除维护标记并重新对齐权威状态。
8. **前后端超时与失败反馈**：清空调用单独放宽等待时间；断线后仍可通过持久化意图重试。用户列表的失败提示固定显示，不会随长列表滚出屏幕。

不增加对话中的 Person 选择器，不引入 Memory 自己的声纹阈值或 controlled_trial 业务开关，不接入多模态长期记忆。

## 自动化验证及边界

- Data 完整 component/integration：276 passed；新增清空意图目录字段后相关目录/API：33 passed。
- Memory 定向单元/功能/真实进程 E2E：101 passed；补充维护竞争测试后 management-mode：7 passed。
- Admin 公共管理、跨进程 Owner/Person 记忆调用、Person 生命周期和面板：111 passed。
- Mobile 管理、记忆浏览/导出/遗忘、窄屏归档恢复：68 passed；APK 构建成功。
- OpenAPI 与生成的 Dart/TypeScript 契约一致。
- 真实 Memory worker + 临时 NATS 验证：暂停管理 HTTP 鉴权、MCP 拒绝、无 turn/sync 消费者；只清理目标 Realm 的消息和消费者；真实 MemPalace 写入→备份→移除→恢复→检索一致。
- Admin→Memory HTTP E2E：默认、暂停 A、启用 B 各自独立读取；外部 Owner/未知 Person 拒绝；A 的令牌不能用于 B；遗忘 A 不影响默认/B。该夹具的存储和异步 worker 使用替身，实际命令处理及真实存储/队列另有上述测试覆盖。
- Local API→Admin→独立 Data 进程 E2E：鉴权、归档、清空失败、pending 阻止恢复、重试、重复确认、恢复保持停用；Memory 清空故障用 HTTP 替身注入。

这些测试不替代真人声纹辨识准确率与真实手机连续对话验收；识别服务的算法和发布策略仍由对应会话负责。


## 现有 Host 发布验证

- Data 0015 部署后发现 4 个旧版硬删除 Person 遗留的私人 Realm。新增 0016 一次性数据迁移，只为这些确实有持有数据的身份补建 deleted=true、inactive 归档，私人 Realm 同时保持 inactive。没有姓名来源时显示“已删除用户（历史档案）”，不猜测身份，不恢复声纹、人脸或授权。迁移含重复执行、默认空间不变、归档可清理测试：14 passed。
- 首轮实际服务探测：默认 1 个、启用个人 1 个、停用个人 4 个，全部 management_reachable；每空间 library/graph/entries/export/recollections，合计 30 次 HTTP 200。
- 发布前实际备份仅含 2 个启用 Realm；发布后实际备份含全部 6 个 Realm，证明停用空间不再漏出 Memory 备份目录。该结果仅证明 Memory 空间覆盖，Ops 仍明确报告 complete_host_backup=false：媒体对象、完整 JetStream、会话状态、部署证据不在现有整机备份范围，本次没有将其误报为完整整机恢复方案。
- 发布使用原有 Mac Host 与原有数据；自动化故障、清空与恢复测试只针对临时测试服务/目录，没有清空现网任何用户的记忆。
- APK 已构建；发布时 ADB 列表为空，未安装到手机，因此手机界面人工验收仍待设备重新连接。
