# “你的记忆”409：Realm 路由修复

日期：2026-10-28。

## 原因

Mobile 的 `GET /api/management/v1/memory/library` 到达 Local API 后返回 409，并非网络断连。Data 已允许 Owner 默认 Realm 与 Person 私有 Realm 共存，但 Memory registry/discovery 丢失了 Realm 类型，Admin 仍按“Owner 只能有一个 Realm”执行唯一性检查，导致个人记忆启用后默认记忆管理不可用。属于多人物 Realm 改造遗漏的跨服务适配，不是 MemPalace 引擎失败。

此前服务健康与 app-ready 验证未覆盖此业务入口，不能据此声称全部产品链路可用。

## 修复边界

Data runtime roster 从权威 `memory_realms.person_id` 推导 `scope=owner|person`；Memory registry 保留类型，Discovery 显式发布；Admin 共用 `_space_for` 只在当前 Owner 的 `scope=owner` 集合中解析默认 Realm。

未知或缺少类型不能被当成默认 Realm。只有个人 Realm 时不能回退读取个人空间；多个默认 Realm 仍拒绝。浏览、搜索、图谱、条目、导出与遗忘均复用同一解析入口。Person 私有记忆不合并到 Owner 默认记忆页；本次不新增个人记忆浏览选择器，也不改变会话按识别结果访问私人 Realm 的规则。

## 验证与部署

- Memory registry/discovery/static：42 passed，含个人类型保留、缺失/未知类型拒绝。
- Admin Memory 客户端：38 passed，含个人 Realm 排在前面、仅个人 Realm、不明确类型、重复默认 Realm。
- Admin 对手机公开的管理路由：83 passed。
- Data integration 初跑 127 passed、1 项新字段断言待更新；更新后相关文件 32 passed。
- 现有 Host 实际 Discovery 返回 owner、person 两类。
- 通过真实 Admin→Discovery→Realm 请求，library、graph、entries、export、recollections 均 HTTP 200；library 明确为 owner scope。未执行生产遗忘写操作。
- 部署后 app-ready 17/17。未自动操作手机页面，用户可退出“你的记忆”后重新进入验收。

部署提交：Data `4dd7f82`、Memory `48be05e`、Admin `fd41997`。后端更新，无需重装 APK。
