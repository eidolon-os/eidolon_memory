"""Rule-based memory steward used as local fallback."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from eidolon_memory_contracts import ConversationTurnPayload

from eidolon.memory.application.claim_routing import PROFILE_RE, TEMPORAL_EVENT_RE
from eidolon.memory.application.forget import extract_privacy_target
from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.application.steward.common import (
    apply_privacy_actions,
    finalize_fragments,
    safe_room_token,
)
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.fragments import MemoryFragment
from eidolon.memory.domain.steward import PrivacyAction, StewardDecision

if TYPE_CHECKING:
    from eidolon.memory.domain.ports import MemoryBackend

SMALLTALK_RE = re.compile(r"^(你好|嗨|哈喽|hello|hi|早安|晚安|谢谢|嗯嗯|好的|ok)[。！!.\s]*$", re.I)

PRIVACY_RE = re.compile(
    r"(不要记住|别记|别记录|不用记|忘掉|删掉|删除|抹掉|不要再提|以后别再提|以后别提|别再提|别再说)"
)
INTERACTION_RE = re.compile(
    r"(叫我|昵称|专属梗|不要[说道]教|别爹|抱我|语气|希望你|对AI|跟AI|助手你|机器人你|人机|陪我)"
)
FUTURE_RE = re.compile(
    r"(梦想|目标|愿望|想去|bucket|清单|计划|三年内|五年内|将来|立志|新年|决心|考研|上岸|开一家|开一间)",
    re.I,
)
RELATION_RE = re.compile(
    r"(妈妈|爸爸|母亲|父亲|伴侣|老婆|老公|男朋友|女朋友|朋友|同事|孩子|宠物|猫|狗)"
)
EMOTION_RE = re.compile(r"(难过|焦虑|崩溃|开心|压力|孤独|害怕|委屈|失落|抑郁|兴奋|安心)")
WORK_RE = re.compile(r"(项目|会议|任务|deadline|同事|客户|老板|工作|学习|考试|论文|需求|bug)", re.I)
HEALTH_RE = re.compile(
    r"(睡眠|失眠|生病|头痛|胃痛|运动|用药|医院|健康|疲惫|确诊|诊断|心理医生|诊疗)"
)
PREFERENCE_RE = re.compile(r"(我喜欢|我讨厌|我习惯|我希望|我偏好|不喜欢|爱吃|喜欢吃)")
DEVICE_RE = re.compile(
    r"(这台设备|这个设备|本设备|客厅|卧室|书房|厨房|车机|车上|汽车|音箱|麦克风|摄像头|屏幕|校准|音量)"
)
CAPABILITY_RE = re.compile(r"(麦克风|摄像头|屏幕|音箱|控制灯|传感器|硬件|能力|校准|音量)")


class RuleBasedSteward:
    """Deterministic steward for privacy safety and LLM fallback."""

    def __init__(self, settings: MemorySettings) -> None:
        self._settings = settings

    @property
    def extraction_version(self) -> str:
        """Bump when deterministic extraction semantics change."""
        return "rules:v2"

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        """Decide, and stamp who decided.

        One stamp site rather than one per ``return`` — there are four inside
        ``_decide`` and a fifth would otherwise be a silent omission.
        """

        return (await self._decide(turn)).stamped_by(self.extraction_version)

    async def _decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        text = f"{turn.user_text}\n{turn.assistant_text}".strip()
        timestamp = turn.timestamp or datetime.now(UTC).isoformat()
        privacy_actions = self._privacy_actions(turn.user_text)
        if privacy_actions:
            return StewardDecision(
                should_write=False,
                reason="用户表达了禁记、删除或不要再提的隐私要求。",
                fragments=[],
                privacy_actions=privacy_actions,
            )
        if not text or SMALLTALK_RE.match(turn.user_text.strip()):
            return StewardDecision(
                should_write=False,
                reason="对话主要是寒暄或没有长期记忆价值。",
            )

        fragment = self._build_fragment(turn, timestamp=timestamp)
        if fragment.importance < self._settings.steward.min_importance_to_write:
            return StewardDecision(
                should_write=False,
                reason="内容信号较弱，低于最小写入重要性阈值。",
            )
        fragments = finalize_fragments(
            [fragment],
            steward="rules",
            context=turn.context,
            source_turn_id=turn.turn_id,
        )
        return StewardDecision(
            should_write=True,
            reason="规则管家识别到可用于未来陪伴的个人记忆。",
            fragments=fragments,
        )

    async def handle_turn(
        self,
        turn: ConversationTurnPayload,
        backend: MemoryBackend,
        kg: Any = None,
    ) -> None:
        """Decide and apply, for callers that are not the turn worker.

        ``kg`` mirrors what ``process_turn_message`` passes. It is not the
        production path — that one calls ``apply_privacy_actions`` itself — but
        the two must not disagree about whether a forget reaches the graph.
        """

        decision = await self.decide(turn)
        await apply_privacy_actions(
            backend,
            memory_space_id=turn.context.memory_space_id,
            actions=decision.privacy_actions,
            kg=kg,
        )
        if not decision.should_write:
            return
        for fragment in decision.fragments:
            await ingest_memory_fragment(backend, fragment)

    def _privacy_actions(self, user_text: str) -> list[PrivacyAction]:
        if not PRIVACY_RE.search(user_text):
            return []
        if re.search(r"(忘掉|删掉|删除|抹掉)", user_text):
            action = "delete_request"
        elif re.search(r"(不要再提|以后别再提|以后别提|别再提|别再说)", user_text):
            action = "archive_topic"
        else:
            action = "do_not_store"
        target = extract_privacy_target(user_text)[:80] or "未命名隐私话题"
        return [
            PrivacyAction(
                action=action,
                target=target,
                reason="用户在对话中明确表达了隐私或记忆控制意图。",
            )
        ]

    def _build_fragment(
        self,
        turn: ConversationTurnPayload,
        *,
        timestamp: str,
    ) -> MemoryFragment:
        text = turn.user_text.strip()
        ctx = turn.context
        has_device = bool(ctx.device_id)
        scope = "device" if has_device and DEVICE_RE.search(text) else "persona"
        visibility = "current_device" if scope == "device" else "all_devices"
        extensions = _extensions_for_text(text)
        if INTERACTION_RE.search(text):
            wing = "Wing_Interaction"
            memory_type = "interaction"
            room = safe_room_token(_first_match(INTERACTION_RE, text), prefix="interaction")
            importance = 4
        elif FUTURE_RE.search(text):
            wing = "Wing_Future"
            memory_type = "goal"
            room = safe_room_token(_first_match(FUTURE_RE, text), prefix="future")
            importance = 4
        elif RELATION_RE.search(text):
            wing = "Wing_Relationship"
            memory_type = "relationship"
            room = safe_room_token(_first_match(RELATION_RE, text), prefix="person")
            importance = 4
        elif EMOTION_RE.search(text):
            wing = "Wing_Emotion"
            memory_type = "emotion"
            theme = _first_match(EMOTION_RE, text)
            room = safe_room_token(f"{theme}_{timestamp[:7]}", prefix="emotion")
            importance = 4
        elif WORK_RE.search(text):
            wing = "Wing_Work"
            memory_type = "work"
            room = safe_room_token(_first_match(WORK_RE, text), prefix="project")
            importance = 4
        elif HEALTH_RE.search(text):
            wing = "Wing_Health"
            memory_type = "health"
            room = safe_room_token(_first_match(HEALTH_RE, text), prefix="health")
            importance = 4
        elif PREFERENCE_RE.search(text):
            wing = "Wing_Life"
            memory_type = "preference"
            room = "preference_life"
            importance = 4
        elif TEMPORAL_EVENT_RE.search(text):
            wing = "Wing_Event"
            memory_type = "event"
            room = safe_room_token(
                _first_match(TEMPORAL_EVENT_RE, text),
                prefix="event",
            )
            importance = 4
        elif PROFILE_RE.search(text):
            wing = "Wing_Profile"
            memory_type = "profile"
            room = "profile_background"
            importance = 4
        else:
            wing = "Wing_Life"
            memory_type = "life"
            room = "event_general"
            importance = 3
        return MemoryFragment(
            memory_space_id=ctx.memory_space_id,
            scope=scope,
            visibility=visibility,
            source_device_id=ctx.device_id,
            target_device_id=ctx.device_id if scope == "device" else None,
            source_instance_id=ctx.companion_id,
            wing=wing,
            room=room,
            content=f"用户提到：{text}",
            memory_type=memory_type,
            importance=importance,
            confidence=0.65,
            occurred_at=timestamp,
            source_turn_id=turn.turn_id,
            session_id=ctx.session_id,
            tags=[memory_type],
            metadata=turn.metadata or {},
            extensions=extensions,
        )


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0) if match else "general"


def _extensions_for_text(text: str) -> dict[str, dict]:
    extensions: dict[str, dict] = {}
    location_re = re.compile(r"(客厅|卧室|书房|厨房|车上|车机|汽车)")
    if location_re.search(text):
        extensions["location"] = {
            "room": _first_match(location_re, text),
            "confidence": 0.8,
        }
    if re.search(r"(车机|车上|汽车|驾驶|开车|座椅|导航)", text):
        extensions["vehicle"] = {"driving_context": True}
    if CAPABILITY_RE.search(text):
        extensions["capability"] = {"raw": _first_match(CAPABILITY_RE, text)}
    return extensions
