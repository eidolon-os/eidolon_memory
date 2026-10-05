# 标注修订与抽取语言对照 — 2026-10-05

本轮只修正可核对的标签错误和抽取提示词中的语言矛盾。未增加查询分类、翻译字典、召回通道或第二份事实源；未迁移已有数据。

## 标签与冻结响应

修订 `quality_queries.jsonl` 的五题，原问题、ID 和分类保持不变：

- canonical-006 北京出差、future-002 对妈妈的承诺、time-004 明确承诺、event-003 与客户吵架：40 轮用户语料均无支持，改为检索边界拒答题。提示词示例不属于用户事实；项目被退回不等于与客户吵架。
- kinship-005：c-007 狗铁锤两岁，c-009 猫小白三岁。年龄命中要求“两岁/2岁”，单有狗名不再算回答了年龄。

使用 `quality-current-20261005/raw_results.json.gz` 原响应，通过原评分函数重新计算，**没有重新召回**。旧标签 16/48（33.3%）→ 修订标签 14/48（29.2%），拒答从 0/5 → 0/9。下降是口径校正，不是系统退化；不能与提示词修订合并宣称效果。逐题变化和输入哈希见 `rescore-results.json`，复现：`.venv/bin/python benchmarks/results/quality-label-language-20261005/rescore.py`。

这不是整套标签审计完成：时间题缺少固定时间锚点、代词题缺少查询上下文、旧实体类型标签及敏感证据访问条件仍需逐题审计。当前分数只作内部诊断，不作产品准确率。

## 语言修订与真实模型对照

生产提示词要求保留用户语言，却用 coffee / insomnia / music_at_night 等英文对象示范中文输入。统一为中文示例，并明确 self / mother 等固定实体 ID 和 person: 等类型前缀保留原协议。既有 extraction_version 会随提示词哈希改变，不新增版本机制。

沿用已授权测试语料的 c-001（失眠）、c-021（茶/咖啡）、c-023（家庭聚会），每组新旧提示词各两次，合计 12 次 DeepSeek 调用；第二轮倒置顺序。配置模型为 `openai/deepseek-v4-flash`；精确模型名、temperature、提示词哈希、完整解析后决定见 `probe-results.json`。脚本使用现有 LiteLLMSteward、配置的 prompt_template_path 和 fact_sentence，无存储写入。运行命令：`.venv/bin/python benchmarks/results/quality-label-language-20261005/probe.py`。旧提示词取自 4384438；实验在该提交上的开发修改中运行，非发布基线。

结果：12/12 完成，无错误。c-001 旧提示词 2/2 为 `mother / has_state / insomnia`，新提示词 2/2 为 `mother / has_state / 失眠`；投影相应从 `mother 处于状态 insomnia` 变为 `mother 处于状态 失眠`。c-021 两组各两次均保留中文乌龙茶/咖啡。

c-023 旧两次和新一次生成“用户参加了家庭聚会”，新另一次只写片段。这显示抽取仍有波动，且生成 triple 时正文仍没有张丽/红烧肉，不能声称完整保真。这里只验证上游语言修正，未测修订后 48 题端到端召回或旧记录修复，也未验证英文历史对象的失效衔接。

## 验证和下一步

81 项现有测试通过：steward_llm、steward_eval_scoring、memory_quality_benchmark、canonical_projection_paths、predicates；48 条标签通过现有 schema 校验；Ruff 和 diff 检查通过。没有添加仅匹配提示词字面的测试。

下一步沿 canonical assertion → evidence → projection 追踪信息保真，先解决结构化事实与支持证据的边界，再验证同一生命周期的撤销、隐私和重放。不能将整段原话追加到每个 triple，也不能恢复并行写入独立 fragments 来掩盖信息损失。旧英文对象与新中文对象是否影响失效匹配，应与这一步一起验证。
