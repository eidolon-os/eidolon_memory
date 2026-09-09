# 模型执行与实时记忆读取的边界

## 问题与证据

2026-09-09 在 opi5max 排查到：对话使用本地 Qwen，记忆抽取使用云端 DeepSeek。
因此“本地模型慢”与“记忆抽取阻塞读取”不能等同。两者会共享主机资源，但这次可复现的
软件问题是 Memory 在首次抽取的事件循环中同步导入 LiteLLM。

独立只读进程实测冷导入 6.65 秒、事件循环阻塞 6.70 秒，热导入约 0.00002 秒。
现场先有 118ms 的正常召回，开始抽取后日志停顿约 17 秒，后续出现 529ms 召回超时和
`memory_route_unreachable`。17 秒没有细粒度 profile，不能全归给导入；同步初始化
足以超过 0.5 秒召回预算和 1.5 秒 discovery 探测预算的机制则已实测。

另一个独立事实是现有 5 条对话全部经过抽取，决策均为 `should_write=false`；向量库和
知识图谱为空，NATS 没有积压、DLQ 没有失败记录。这解释了空库，不能当成写入失败。

## 执行职责

```text
Agent ── MCP ──> Memory runner ──> 唯一 palace / ledger 持有者
                      ↑
Agent ── NATS ──> turn processor
                      │
                      └── 异步 stdio ──> 模型子进程 ──> 本地或云端模型
                             <────────── 模型响应
```

- `LiteLLMSteward` 保留提示词、用户证据校验、身份盖章、阈值和抽取版本语义。
- `IsolatedLLMCompletion` 持有一个按需启动并复用的模型子进程。SDK 冷加载、同步 provider
  代码及 HTTP 调用发生在子进程内。模型在本地还是云端不改变这条边界。
- 子进程不构造存储对象，也不持有存储句柄。只有 runner 写 extraction decision ledger、
  canonical facts、Chroma/KG 投影并确认 NATS 消息。
- 每个 executor 同时只执行一个请求，调用方异步等待形成背压，不新增持久队列或另一套
  写入事实源。并发的 turn/sync 不共享或串错响应。
- 总执行预算使用现有 `llm.timeout_seconds`，包含初始化与 provider 内部重试。
  超时、取消、协议错误或进程退出会回收子进程，失败交回已有 NATS 重投/DLQ 路径，
  不转成“无记忆”或提前 ACK。之后的请求启动新进程，不能收到上一轮迟到的响应。
- runner 的 lifespan 关闭 executor；关闭会中断在途模型请求并等待进程回收。
  SDK 标准输出重定向到日志，响应管道只承载 JSON。凭据不进入命令行。

## 路由与健康

配套 Agent 修复区分 authority 与 observation：Realm 是否存在、是否 enabled 是路由
准入条件；`agent_reachable` 是某次探测的观测结果，不是禁用 Realm 的决定。

探测失败仍体现在 health 和预热选择中，但不能销毁正在工作的 MCP 会话或阻止下一次
有预算的实际读取。真实请求失败仍按调用超时/传输错误降级；删除或禁用 Realm 仍拒绝
访问。没有延长召回预算，也没有把故障探测改成永远成功。

## 验证与限制

`test_llm_process.py` 使用真实子进程和会同步阻塞的模拟 SDK，覆盖冷加载、慢调用、
本地/云端配置透传、进程复用、并发背压、超时、取消、关闭、崩溃和后续恢复。

`e2e/test_model_execution_isolation.py` 使用独立 NATS、真实 runner/MCP 和临时 palace，
在 SDK 冷加载及模型调用各阻塞 3 秒时验证读取与 MCP 初始化探测仍能在 0.5 秒内完成，
随后验证“抽取 → canonical ledger / 投影 → 读取 → 语音召回”。模型响应是确定性 fixture，
不是在线模型质量测试。

本次验证结果：Memory 非端到端回归 1181 通过、5 跳过；Agent memory 适配层 53 通过；
模型隔离和慢抽取不阻塞命令的两个 MCP/NATS 端到端用例通过。非端到端回归包含真实
LiteLLM SDK 对本地临时 HTTP 模型端点的验证，不消耗云端模型调用。

这解决执行隔离，不承诺在主机 CPU/内存耗尽时仍有固定延迟，也不让尚未完成抽取的事实
提前可见。增加一个轻量子进程有资源开销；SDK 在子进程复用，读取进程不加载第二份 SDK。
设备发布需同时包含 Memory 和 Agent 修复。
