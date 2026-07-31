"""LiteLLM-backed memory steward."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from eidolon_memory_contracts import ConversationTurnPayload
from pydantic import ValidationError

from eidolon.memory.application.ingest import ingest_memory_fragment
from eidolon.memory.application.steward.common import apply_privacy_actions, finalize_fragments
from eidolon.memory.application.steward.rules import RuleBasedSteward
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import StewardOutputError
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.support.logging import get_logger

if TYPE_CHECKING:
    from eidolon.memory.domain.ports import MemoryBackend

log = get_logger(__name__)


class LiteLLMSteward:
    """Steward that asks a local OpenAI-compatible model to extract memories."""

    def __init__(
        self,
        settings: MemorySettings,
        *,
        fallback: RuleBasedSteward | None = None,
    ) -> None:
        self._settings = settings
        self._fallback = fallback or RuleBasedSteward(settings)

    @property
    def extraction_version(self) -> str:
        """Identify the configured extraction policy, including fallback semantics."""
        policy = {
            "model": self._settings.llm.model,
            "prompt": self._settings.render_steward_prompt(),
            "temperature": self._settings.llm.temperature,
            "fallback_to_rules": self._settings.steward.fallback_to_rules,
            "fallback_version": self._fallback.extraction_version,
            "max_fragments": self._settings.steward.max_fragments_per_turn,
            "min_importance": self._settings.steward.min_importance_to_write,
        }
        raw = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"llm:{digest}"

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        try:
            if not self._settings.llm.model:
                msg = "llm.model is not configured in memory settings YAML"
                raise StewardOutputError(msg)
            raw = await self._call_llm(turn)
            decision = self._parse_decision(raw)
            decision.fragments = [
                f
                for f in decision.fragments[: self._settings.steward.max_fragments_per_turn]
                if f.importance >= self._settings.steward.min_importance_to_write
            ]
            decision.fragments = finalize_fragments(
                decision.fragments,
                steward="llm",
                context=turn.context,
                source_turn_id=turn.turn_id,
            )
            if decision.fragments:
                decision.should_write = True
            return decision
        except Exception as exc:
            if not self._settings.steward.fallback_to_rules:
                raise
            log.warning("llm_steward_fallback_to_rules", error=str(exc))
            return await self._fallback.decide(turn)

    async def handle_turn(self, turn: ConversationTurnPayload, backend: MemoryBackend) -> None:
        decision = await self.decide(turn)
        await apply_privacy_actions(
            backend,
            memory_space_id=turn.context.memory_space_id,
            actions=decision.privacy_actions,
        )
        if not decision.should_write:
            return
        for fragment in decision.fragments:
            await ingest_memory_fragment(backend, fragment)

    async def _call_llm(self, turn: ConversationTurnPayload) -> str:
        from litellm import acompletion

        messages = [
            {"role": "system", "content": self._settings.render_steward_prompt()},
            {"role": "user", "content": self._render_user_prompt(turn)},
        ]
        kwargs: dict[str, Any] = {
            "model": self._settings.llm.model,
            "messages": messages,
            "temperature": self._settings.llm.temperature,
            "timeout": self._settings.llm.timeout_seconds,
        }
        if self._settings.llm.base_url:
            kwargs["api_base"] = self._settings.llm.base_url
        api_key = self._settings.llm.resolve_api_key()
        if api_key:
            kwargs["api_key"] = api_key
        response = await acompletion(**kwargs)
        return _extract_content(response)

    def _render_user_prompt(self, turn: ConversationTurnPayload) -> str:
        meta = json.dumps(turn.metadata or {}, ensure_ascii=False)
        context = turn.context.model_dump(mode="json")
        return (
            "请分析下面这一轮对话并输出 JSON。\n\n"
            f"turn_id: {turn.turn_id}\n"
            f"context: {json.dumps(context, ensure_ascii=False)}\n"
            f"timestamp: {turn.timestamp}\n"
            f"metadata: {meta}\n\n"
            "每个 fragments[] 必须包含 memory_space_id, scope, visibility, "
            "source_device_id, target_device_id, source_instance_id, source_turn_id, "
            "session_id, wing, room, content, memory_type, importance, confidence, "
            "metadata, extensions。身份字段会由服务端 runtime context 覆盖，"
            "不要依赖模型生成 owner_id、companion_id 或 memory_realm_id。\n"
            "scope 只能是 global/persona/agent/device/session；设备位置、能力、校准、"
            "本地环境用 scope=device visibility=current_device；用户长期偏好、关系、"
            "事实用 scope=persona visibility=all_devices。\n\n"
            f"[USER]\n{turn.user_text}\n\n"
            f"[ASSISTANT]\n{turn.assistant_text}\n"
        )

    def _parse_decision(self, raw: str) -> StewardDecision:
        try:
            data = json.loads(_strip_json_fence(raw))
            return StewardDecision.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            msg = f"invalid LLM steward output: {exc}"
            raise StewardOutputError(msg) from exc


def _extract_content(response: Any) -> str:
    if isinstance(response, dict):
        return str(response["choices"][0]["message"]["content"])
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if isinstance(message, dict):
            return str(message.get("content", ""))
        return str(getattr(message, "content", ""))
    return str(response)


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return match.group(1).strip() if match else text
