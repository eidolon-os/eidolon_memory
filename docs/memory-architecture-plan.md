# Eidolon Memory 详细架构与实现计划

## Summary

`eidolon.memory` 是局域网、桌面和家居智能陪伴体的长期记忆服务。底层长期使用 MemPalace，但本项目保留一层薄适配层，用来稳定“陪伴记忆”的业务语义：个人事实、情绪、事件、关系、工作内容、隐私禁记、封存和召回策略。

```text
Companion Agent
  -> Eidolon Memory MCP Read Server
      -> Recall Application Service
          -> MemPalace Adapter
              -> MemPalace Python API

Companion Agent
  -> NATS JetStream conversation turn
      -> Memory Worker
          -> LLM / Rule Steward
              -> MemoryFragment[]
                  -> MemPalace Adapter
                      -> MemPalace Python API
```

核心决策：

- 读：本项目暴露自有 MCP tools，内部直接调用 MemPalace Python API。
- 写：以 JetStream worker 异步写为主，避免对话热路径等待 LLM 或 MemPalace。
- LLM：使用 LiteLLM 接 OpenAI-compatible endpoint，默认适合本地 Ollama、LM Studio、vLLM、llama.cpp server。
- 隐私：强隐私优先；用户表达“别记住、忘掉、不要再提”时，不写入普通记忆，并生成封存或删除意图。
- MemPalace：长期作为唯一底层存储，内部只使用 Python API；wing、room、drawer 设计由 `eidolon.memory` 统一治理。

## MemPalace 信息架构

层级约定：

```text
Palace = 单个用户或单个家庭/本机实例的记忆宫殿根目录
Wing   = 高层记忆领域
Room   = 稳定主题、人物、项目、情绪线索或生活领域
Drawer = 一条可召回的具体记忆片段
```

默认 palace path（当 YAML 中 `runtime.palace_path` 为空时）：

- `~/eidolon/mempalace`

默认 wings（以 `memory.bundled.yaml` 为准，可本地用 `memory.default.yaml` 覆盖）：

- `Wing_Profile`：个人画像与价值观（静态身份、观点）。
- `Wing_Interaction`：人机羁绊与交互偏好。
- `Wing_Relationship`：人际关系与长期关系状态。
- `Wing_Emotion`：情绪与心理压力趋势。
- `Wing_Future`：愿景与未来目标。
- `Wing_Event`：带时间戳的情景记忆节点。
- `Wing_Work`：工作与学习产出与压力。
- `Wing_Life`：生活方式与财务等日常动态。
- `Wing_Health`：生理与医学心理健康记录。
- `Wing_Privacy`：禁记与封存规则，普通召回过滤。

Room 命名规范：

- `profile_core`：用户核心画像。
- `person_<name_or_alias>`：重要人物。
- `pet_<name_or_alias>`：宠物。
- `project_<project_name>`：工作项目。
- `emotion_<theme>_<yyyy_mm>`：情绪主题。
- `event_<short_topic>`：重要事件。
- `preference_<category>`：偏好。
- `privacy_<topic>`：禁记或封存主题。

Drawer 内容规范：

- 每条 drawer 是一条独立可召回记忆，不写超长流水账。
- 内容使用自然中文短句。
- 不把助手推测写成事实。
- metadata 必须携带来源、置信度、重要性、记忆类型、隐私状态和来源 turn。

## LLM Steward

LLM steward 输入为一轮完整对话 `ConversationTurnPayload`，输出为 `StewardDecision`：

```json
{
  "should_write": true,
  "reason": "string",
  "fragments": [
    {
      "fragment_id": "",
      "user_id": "string",
      "wing": "Wing_Profile",
      "room": "profile_core",
      "content": "用户喜欢在晚上独处时听轻音乐放松。",
      "memory_type": "preference",
      "importance": 4,
      "confidence": 0.9,
      "occurred_at": "2026-05-14T20:00:00+08:00",
      "source_turn_id": "string",
      "session_id": "string",
      "tags": ["音乐", "放松"],
      "privacy": "normal",
      "metadata": {}
    }
  ],
  "privacy_actions": [
    {
      "action": "archive_topic",
      "target": "前任相关话题",
      "reason": "用户明确表示不要再提。"
    }
  ]
}
```

写入标准：

- 保存个人事实、长期偏好、重要关系、情绪峰值、持续压力源、工作/学习事项、重要事件。
- 跳过纯寒暄、一次性闲聊、助手猜测、用户未确认的推断。
- 高敏感内容默认不写，除非用户明确要求长期记住。
- `importance < min_importance_to_write` 的 fragment 默认丢弃。
- `fragment_id = sha256(user_id + source_turn_id + index + normalized_content)`。

失败策略：

- LLM 调用异常、超时、非法 JSON 或 schema 校验失败时，默认 fallback 到规则管家。
- 规则管家至少能处理寒暄跳过、关系、工作、情绪、偏好和隐私禁记。

## 隐私与封存

强隐私优先：

- “不要记住 / 别记录”：生成 `do_not_store`，不写普通 fragment。
- “忘掉 / 删掉”：生成 `delete_request`。
- “不要再提”：生成 `archive_topic`。

如果 MemPalace adapter 收到非 drawer_id 的删除请求，系统必须记录明确 warning，并避免把相关内容作为普通记忆继续写入。普通召回默认过滤：

- `privacy in ["private", "do_not_recall"]`
- `room_status in ["taboo", "archived"]`
- `wing == "Wing_Privacy"`

## Interfaces

对外 MCP tools：

- `eidolon_memory_search(query, user_id, top_k, wing?, room?)`
- `eidolon_memory_recall_context(query, user_id, top_k)`
- `eidolon_memory_status()`

写入主路径：

- Agent 发布 `ConversationTurnPayload` 到 JetStream。
- Worker 消费后调用 steward。
- Steward 生成 `MemoryFragment[]`。
- Backend 写入 MemPalace。
- 成功 ACK，backend 写失败 NAK，坏 payload ACK 并记录错误。

## Acceptance Criteria

- 完整测试通过：`uv run pytest tests -q`。
- `get/get_all` 不再在 MemPalace adapter 中伪成功。
- worker 默认不再直接使用 `NoOpSteward`。
- LLM steward 有完整 prompt、schema 校验和 fallback。
- 默认 `memory.bundled.yaml`（及可选本地 `memory.default.yaml`）中的 wings 与召回策略适合个人陪伴记忆。
- README 链接本文档，并和实际运行方式一致。
- MemPalace Python API 细节不泄露给智能体主系统。
