"""Offline-only LiteLLM proposer for Commitment shadow evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

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

规则：
- 目标、愿望、玩笑、反事实、引用他人或明确否认承诺，输出 none。
- create 表示本轮产生了新的明确承诺；不能带 target_candidates。
- supplement/fulfil/cancel/supersede 必须关联输入中的 active commitment，
  最多返回 5 个候选，按置信度降序。
- 无法可靠区分多个 target 时输出 none，不要猜。
- evidence_quote 必须逐字来自本轮文本；none 时可以为空。
- 不输出 MemoryIntent，不决定写入、履约、取消或 supersede，只给离线评测建议。
"""


class LiteLLMCommitmentShadowProposer:
    """Call one configured model without registering in the runtime worker."""

    def __init__(self, llm: LlmConfig) -> None:
        self._llm = llm

    @property
    def extraction_version(self) -> str:
        payload = json.dumps(
            {
                "model": self._llm.model,
                "base_url": self._llm.base_url,
                "temperature": 0.0,
                "prompt": _SYSTEM_PROMPT,
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
        try:
            payload = json.loads(_strip_json_fence(raw))
            candidate = CommitmentShadowCandidate.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CommitmentShadowOutputError(
                f"invalid commitment shadow output: {exc}"
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


__all__ = ["LiteLLMCommitmentShadowProposer"]
