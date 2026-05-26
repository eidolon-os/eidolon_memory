"""Canonical wing taxonomy — product contract, not configuration.

The 9 wings + ``Wing_Privacy`` are a fixed semantic schema baked into the
steward prompt, the KG predicate model, the recall router and the admin
visualizations. Letting a yaml file redefine them would make every
deployment a different product.

If you need a new wing, add it here, update the steward prompt + tests,
ship a release. Do NOT expose this as user-tunable.

Read by:
- :func:`MemorySettings.wings` (read-only property)
- :func:`MemorySettings.wings_prompt_block` (steward prompt rendering)
- recall fan-out, hierarchy builder, runtime warm, MCP status tool
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel


class WingDefinition(BaseModel):
    """A semantic wing (top-level category in the mempalace).

    Pydantic-validated for parity with previous yaml-loaded shape;
    instances live as a frozen tuple in :data:`CANONICAL_WINGS`.
    """

    id: str
    display_name: str = ""
    description: str = ""
    sort_order: int = 0


CANONICAL_WINGS: Final[tuple[WingDefinition, ...]] = (
    WingDefinition(
        id="Wing_Profile",
        display_name="个人画像与价值观",
        description=(
            "静态/核心的身份认同与客观背景：籍贯、原生家庭阶层、MBTI、核心价值观、宗教信仰、"
            "政治与社会观点、对婚姻/生育等议题的立场。不包含「每天喝美式」这类动态日常偏好"
            "（归 Wing_Life）。"
        ),
        sort_order=1,
    ),
    WingDefinition(
        id="Wing_Interaction",
        display_name="人机羁绊与交互偏好",
        description=(
            "正向的人机关系：专属昵称与梗、希望 AI 扮演的语气与角色（如不要爹味说教、难过时"
            "先共情少讲理）、对历史回复的点赞/批评。与 Wing_Privacy（负向「不要记/别提」清单）区分。"
        ),
        sort_order=2,
    ),
    WingDefinition(
        id="Wing_Relationship",
        display_name="关系与情感连接",
        description=(
            "重要他人的人物图谱与**长期关系状态**（如与父亲疏远、与伴侣的相处模式）。"
            "「今天和父亲大吵一架」等带明确时间点的单次现场归 Wing_Event；此处存可跨时间概括的关系语义。"
        ),
        sort_order=3,
    ),
    WingDefinition(
        id="Wing_Emotion",
        display_name="情绪与心理状态",
        description=(
            "日常情绪起伏、持续压力源、脆弱与安全感、依恋模式与心理情绪趋势（未医学定性的主观感受）。"
            "已确诊并进入治疗/用药管理的心理健康议题归 Wing_Health。"
        ),
        sort_order=4,
    ),
    WingDefinition(
        id="Wing_Future",
        display_name="愿景与未来目标",
        description=(
            "面向未来的语义记忆：新年决心、Bucket List、中长期自我提升与阶层/财务目标、想去尚未去的地方；"
            "便于在用户迷茫时主动鼓励与对齐计划。"
        ),
        sort_order=5,
    ),
    WingDefinition(
        id="Wing_Event",
        display_name="事件与人生片段",
        description=(
            "**情景记忆 Episodic**：时间或场景锚定的一次性节点——纪念日、旅行片段、当场冲突、"
            "里程碑成就现场等。与 Wing_Relationship / Wing_Work 中的「长期状态与角色」区分："
            "此处记「发生了什么」，彼处记「关系/职业格局」。"
        ),
        sort_order=6,
    ),
    WingDefinition(
        id="Wing_Work",
        display_name="工作与学习",
        description=(
            "当前项目与任务进展、职业压力、学习计划、专业成就与协作状态；偏社会功能与产出。"
            "与 Wing_Life 的消费/作息区分：此处偏职场与学业语境。"
        ),
        sort_order=7,
    ),
    WingDefinition(
        id="Wing_Life",
        display_name="生活方式与财务",
        description=(
            "动态日常：饮食作息、兴趣爱好、书影音与品牌偏好、居家设备、消费习惯。"
            "房贷压力、投资偏好、大额支出计划、车房等资产与现金流焦虑等与「过日子」强相关的财务语义也可归此。"
        ),
        sort_order=8,
    ),
    WingDefinition(
        id="Wing_Health",
        display_name="身体与健康",
        description=(
            "生理状态、睡眠与运动数据、疾病史、用药与就诊记录；以及**医学框架下**的心理健康"
            "（确诊、治疗方案、心理咨询节律）。日常情绪低落但未进入诊疗路径的叙述归 Wing_Emotion。"
        ),
        sort_order=9,
    ),
    WingDefinition(
        id="Wing_Privacy",
        display_name="隐私与禁记规则",
        description=(
            "用户明确要求不要记住、不要再提、删除或封存的话题，以及创伤禁忌；普通召回默认过滤。"
            "与 Wing_Interaction（正向希望 AI 如何陪伴）互补：此处为负向边界与禁运记忆。"
        ),
        sort_order=99,
    ),
    WingDefinition(
        id="Wing_Theme",
        display_name="主题与走势",
        description=(
            "Consolidator 后台进程的产物：跨时段的高阶主题摘要（如\"近三周担心妈妈失眠 + "
            "工作压力\"）。原始细节仍在其他 wing；Wing_Theme 提供\"远观\"角度，召回时排在"
            "[最近对话]之后、其他 wing 之前。fragment.metadata 必带 source=\"consolidator\" + "
            "underlying_wing 指向源主题所在 wing。"
        ),
        sort_order=10,
    ),
)


CANONICAL_WING_IDS: Final[frozenset[str]] = frozenset(w.id for w in CANONICAL_WINGS)


def get_wing(wing_id: str) -> WingDefinition | None:
    """Return the canonical wing by id, or ``None`` if unknown."""
    for w in CANONICAL_WINGS:
        if w.id == wing_id:
            return w
    return None
