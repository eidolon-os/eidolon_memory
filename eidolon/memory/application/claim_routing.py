"""Deterministic product routing for verbatim memory claims.

Routing chooses a palace location; it never rewrites or infers a claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClaimRoute:
    wing: str
    memory_type: str


TEMPORAL_EVENT_RE = re.compile(
    r"(今天|明天|后天|今晚|昨晚|本周|这周|下周|周[一二三四五六日天末]|"
    r"\d{4}[年/-]\d{1,2}|\d{1,2}月\d{1,2}[日号]?)",
    re.I,
)
FUTURE_RE = re.compile(
    r"(梦想|目标|愿望|bucket|清单|长期计划|三年内|五年内|将来|以后想|立志|新年决心)",
    re.I,
)
PROFILE_RE = re.compile(
    r"(我(?:家)?住在|我居住在|我来自|我老家|我的籍贯|我出生在|我是.+人|家在)",
    re.I,
)
WORK_RE = re.compile(
    r"(工作|上班|公司|职业|项目|会议|任务|deadline|同事|客户|老板|"
    r"学习|学校|考试|论文|需求|bug)",
    re.I,
)
HEALTH_RE = re.compile(
    r"(睡眠|失眠|生病|头痛|胃痛|运动|用药|医院|健康|确诊|诊断|治疗|过敏)",
    re.I,
)
RELATION_RE = re.compile(
    r"(妈妈|爸爸|母亲|父亲|伴侣|老婆|老公|男朋友|女朋友|孩子|宠物|猫|狗)",
    re.I,
)
EMOTION_RE = re.compile(
    r"(难过|焦虑|崩溃|开心|压力|孤独|害怕|委屈|失落|抑郁|兴奋|安心)",
    re.I,
)
INTERACTION_RE = re.compile(
    r"(叫我|昵称|语气|希望你|不要[说道]教|别爹|陪我|助手你|机器人你)",
    re.I,
)
PREFERENCE_RE = re.compile(
    r"(我喜欢|我讨厌|我习惯|我偏好|不喜欢|爱吃|喜欢吃)",
    re.I,
)


def route_explicit_claim(text: str, *, intent_type: str = "fact") -> ClaimRoute:
    """Route a confirmed verbatim claim without adding semantic content."""
    clean = text.strip()
    if intent_type == "commitment":
        return ClaimRoute("Wing_Future", "goal")
    if intent_type == "episode":
        return ClaimRoute("Wing_Event", "event")
    if intent_type == "preference" or PREFERENCE_RE.search(clean):
        return ClaimRoute("Wing_Life", "preference")
    if TEMPORAL_EVENT_RE.search(clean):
        return ClaimRoute("Wing_Event", "event")
    if FUTURE_RE.search(clean):
        return ClaimRoute("Wing_Future", "goal")
    if HEALTH_RE.search(clean):
        return ClaimRoute("Wing_Health", "health")
    if WORK_RE.search(clean):
        return ClaimRoute("Wing_Work", "work")
    if RELATION_RE.search(clean):
        return ClaimRoute("Wing_Relationship", "relationship")
    if EMOTION_RE.search(clean):
        return ClaimRoute("Wing_Emotion", "emotion")
    if INTERACTION_RE.search(clean):
        return ClaimRoute("Wing_Interaction", "interaction")
    if PROFILE_RE.search(clean):
        return ClaimRoute("Wing_Profile", "profile")
    return ClaimRoute("Wing_Profile", "profile")
