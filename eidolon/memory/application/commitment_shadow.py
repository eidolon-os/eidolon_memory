"""Offline-only LiteLLM proposer for Commitment shadow evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import ValidationError

from eidolon.memory.config.memory_settings import LlmConfig
from eidolon.memory.domain.commitment_shadow import (
    CommitmentShadowCandidate,
    CommitmentShadowInput,
    CommitmentShadowOutputError,
    validate_shadow_candidate,
)

_SYSTEM_PROMPT = """你是 Commitment shadow evaluator。你只能解释当前对话，不拥有写权限。

输入包含 owner/companion 本轮文本，以及同一 Memory Realm 中最多 10 条 active Commitment。
只输出一个 JSON object，字段必须符合：
{
  "operation": "none|create|supplement|fulfil|cancel|supersede",
  "promisor": "string",
  "predicate": "promised|committed_to|planned_to|null",
  "action": "string",
  "beneficiaries": ["string"],
  "participants": ["string"],
  "condition": "string|null",
  "due_at": "ISO-8601 string|null",
  "target_candidates": [
    {"commitment_id": "必须来自输入 active 列表", "confidence": 0.0, "reason": "string"}
  ],
  "confidence": 0.0,
  "evidence_quote": "来自本轮 owner 或 companion 文本的原句",
  "reason": "string"
}
所有字段都必须出现。string 字段不得输出 null；不适用时用 ""。

none 格式示例：
{
  "operation": "none",
  "promisor": "",
  "predicate": null,
  "action": "",
  "beneficiaries": [],
  "participants": [],
  "condition": null,
  "due_at": null,
  "target_candidates": [],
  "confidence": 0.9,
  "evidence_quote": "",
  "reason": "不是承诺"
}

命中已有 Commitment 时，例如 fulfil，promisor/predicate/action/beneficiaries 从
active commitment 逐字复制，target_candidates 只包含输入 ID。

规则：
- 目标、愿望、玩笑、反事实、引用他人或明确否认承诺，输出 none。
- user_text 中的“我”是 owner，assistant_text 中的“我”是 companion；
  promisor 只用 owner/companion 或输入 active commitment 已有的精确值。
- create 表示本轮产生了新的明确承诺；不能带 target_candidates。
- action 是 Commitment identity 的完整核心动作；不能丢失原句中无法转为绝对
  due_at 的“明早”、“周六”等相对时间，人称必须规范为 owner/companion。
- supplement 只表示补充 participants/condition/due_at，不改变核心动作；
  promisor/predicate/action/beneficiaries 必须逐字沿用命中的 active commitment。
- fulfil/cancel 必须逐字沿用命中的 active commitment identity 字段。
- supersede 只用于核心 action 被新承诺替代；仅修改时间、参与者或条件是
  supplement，不是 supersede。
- supplement/fulfil/cancel/supersede 必须关联输入中的 active commitment，
  最多返回 5 个候选，按置信度降序。
- 无法可靠区分多个 target 时输出 none，不要猜。
- none 的 target_candidates 必须是 []，即使你曾经比较过多个候选也不得返回它们。
- evidence_quote 必须逐字来自本轮文本；none 时可以为空。
- 不输出 MemoryIntent，不决定写入、履约、取消或 supersede，只给离线评测建议。
"""


class LiteLLMCommitmentShadowProposer:
    """Call one configured model without registering in the runtime worker."""

    def __init__(
        self,
        llm: LlmConfig,
        *,
        thinking: Literal["enabled", "disabled"] = "enabled",
    ) -> None:
        self._llm = llm
        self._thinking = thinking

    @property
    def extraction_version(self) -> str:
        payload = json.dumps(
            {
                "model": self._llm.model,
                "base_url": self._llm.base_url,
                "temperature": 0.0,
                "thinking": self._thinking,
                "thinking_transport": "extra_body",
                "prompt": _SYSTEM_PROMPT,
                "max_tokens": 1200,
                "max_active_commitments": 10,
                "max_target_candidates": 5,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"commitment-shadow:{digest}"

    async def propose(
        self,
        shadow_input: CommitmentShadowInput,
    ) -> CommitmentShadowCandidate:
        if not self._llm.model:
            raise CommitmentShadowOutputError("llm.model is not configured")
        raw = await self._call_llm(shadow_input)
        if not raw.strip():
            raise CommitmentShadowOutputError(
                "provider returned empty commitment shadow output",
                failure_type="provider_empty",
            )
        try:
            payload = json.loads(_strip_json_fence(raw))
        except json.JSONDecodeError as exc:
            raise CommitmentShadowOutputError(
                f"invalid commitment shadow JSON: {exc}",
                failure_type="schema_invalid",
            ) from exc
        try:
            candidate = CommitmentShadowCandidate.model_validate(payload)
        except ValidationError as exc:
            raise CommitmentShadowOutputError(
                f"invalid commitment shadow schema: {exc}",
                failure_type="schema_invalid",
            ) from exc
        return validate_shadow_candidate(shadow_input, candidate)

    async def _call_llm(self, shadow_input: CommitmentShadowInput) -> str:
        from litellm import acompletion

        kwargs: dict[str, Any] = {
            "model": self._llm.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        shadow_input.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            "temperature": 0.0,
            "extra_body": {"thinking": {"type": self._thinking}},
            "max_tokens": 1200,
            "timeout": self._llm.timeout_seconds,
            "response_format": {"type": "json_object"},
        }
        if self._llm.base_url:
            kwargs["api_base"] = self._llm.base_url
        if api_key := self._llm.resolve_api_key():
            kwargs["api_key"] = api_key
        response = await acompletion(**kwargs)
        return _extract_content(response)


def _extract_content(response: Any) -> str:
    if isinstance(response, dict):
        return response["choices"][0]["message"].get("content") or ""
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if isinstance(message, dict):
            return message.get("content") or ""
        return getattr(message, "content", "") or ""
    return str(response)


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return match.group(1).strip() if match else text


__all__ = ["LiteLLMCommitmentShadowProposer"]
