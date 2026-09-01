"""LiteLLM-backed memory steward."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from eidolon_memory_contracts import ConversationTurnPayload
from pydantic import ValidationError

from eidolon.memory.application.steward.common import finalize_fragments
from eidolon.memory.config.memory_settings import MemorySettings
from eidolon.memory.domain.errors import StewardOutputError
from eidolon.memory.domain.fragments import is_usable_extension
from eidolon.memory.domain.steward import StewardDecision
from eidolon.memory.support import metrics
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)


class LiteLLMSteward:
    """Steward that asks a local OpenAI-compatible model to extract memories."""

    def __init__(
        self,
        settings: MemorySettings,
    ) -> None:
        self._settings = settings

    @property
    def extraction_version(self) -> str:
        """Identify the configured semantic extraction policy."""
        policy = {
            "model": self._settings.llm.model,
            "prompt": self._settings.render_steward_prompt(),
            "temperature": self._settings.llm.temperature,
            "max_fragments": self._settings.steward.max_fragments_per_turn,
            "min_importance": self._settings.steward.min_importance_to_write,
        }
        raw = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"llm:{digest}"

    async def decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        """Decide, and stamp who decided.

        Invalid output is a retryable extraction failure. It must not be replaced
        by a keyword-based decision with different semantics.
        """

        return (await self._decide(turn)).stamped_by(self.extraction_version)

    async def _decide(self, turn: ConversationTurnPayload) -> StewardDecision:
        if not self._settings.llm.model:
            msg = "llm.model is not configured in memory settings YAML"
            raise StewardOutputError(msg)
        raw = await self._call_llm(turn)
        decision = self._parse_decision(
            raw,
            context=turn.context,
            source_turn_id=turn.turn_id,
        )
        _validate_user_evidence(decision, turn.user_text)
        proposed = decision.fragments
        capped = proposed[: self._settings.steward.max_fragments_per_turn]
        kept = [f for f in capped if f.importance >= self._settings.steward.min_importance_to_write]
        # Count where material is lost. Without this a corpus that yields few
        # memories looks the same whether the model proposed little or these
        # two thresholds discarded most of what it proposed — and the fix
        # differs completely.
        metrics.FRAGMENTS_EXTRACTED.labels(stage="proposed").inc(len(proposed))
        metrics.FRAGMENTS_EXTRACTED.labels(stage="dropped_cap").inc(len(proposed) - len(capped))
        metrics.FRAGMENTS_EXTRACTED.labels(stage="dropped_importance").inc(len(capped) - len(kept))
        metrics.FRAGMENTS_EXTRACTED.labels(stage="written").inc(len(kept))
        if len(kept) < len(proposed):
            log.info(
                "steward_fragments_filtered",
                turn_id=turn.turn_id,
                proposed=len(proposed),
                written=len(kept),
                dropped_by_cap=len(proposed) - len(capped),
                dropped_by_importance=len(capped) - len(kept),
                min_importance=self._settings.steward.min_importance_to_write,
            )
        decision.fragments = finalize_fragments(
            kept,
            steward="llm",
            context=turn.context,
            source_turn_id=turn.turn_id,
        )
        if decision.fragments:
            decision.should_write = True
        return decision

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
            # Only the fields that survive. The list used to demand five that
            # ``stamp_fragment_identity`` overwrites from the turn — including
            # the two that fail validation when blank — while the sentence after
            # it said identity gets overwritten. Asking for a value in order to
            # discard it is what put a model-supplied ``source_turn_id`` on the
            # path that threw away a whole turn's extraction.
            # ``evidence_quote`` is here because _validate_user_evidence rejects
            # the whole decision without it, and it was in neither version of
            # this list — the system prompt demanded it and this one did not,
            # which is the same contradiction as the identity fields, pointing
            # the other way.
            "每个 fragments[] 必须包含 evidence_quote, scope, visibility, "
            "target_device_id, wing, room, content, memory_type, importance, "
            "confidence, metadata, extensions。\n"
            "fragments/triples/invalidations/privacy_actions 每一项的 "
            "evidence_quote 都必须逐字取自本轮 [USER]，缺失或非原文会让整轮作废。\n"
            "不要输出身份字段：memory_space_id、memory_realm_id、owner_id、"
            "companion_id、source_device_id、source_instance_id、source_turn_id、"
            "session_id 全部由服务端按这一轮的 runtime context 写入，模型给的值"
            "会被丢弃。\n"
            "scope 只能是 global/persona/agent/device/session；设备位置、能力、校准、"
            "本地环境用 scope=device visibility=current_device；用户长期偏好、关系、"
            "事实用 scope=persona visibility=all_devices。\n\n"
            f"[USER]\n{turn.user_text}\n"
        )

    def _parse_decision(
        self,
        raw: str,
        *,
        context: object | None = None,
        source_turn_id: str = "",
    ) -> StewardDecision:
        """Validate the model's JSON, after taking back the fields that are ours.

        Which memory space a fragment belongs to is decided by the turn, not by the
        model — ``finalize_fragments`` overwrites it from the context after this
        returns. But validation ran first and
        ``MemoryFragment.memory_space_id`` rejects a blank one, so a model that
        omitted the field failed the whole decision over a value we were about to
        replace. The turn then failed extraction and had to be retried from the
        durable turn stream.

        Observed once in 40 turns, and not at all in the 160 turns before it — the
        kind of rate that makes a benchmark irreproducible rather than obviously
        broken.

        So the field is set from the context here rather than merely defaulted:
        replacing it means a model that invents a *different* space id cannot get
        one past validation either. That path is already safe — finalization
        overwrites unconditionally — and this keeps it safe without depending on
        the order of two functions.

        ``source_turn_id`` is the same field with a different name, found the
        same way and only later: a live run took 55.8s to materialise one turn,
        and the log said 38s of that was an LLM call thrown away because the
        model returned ``fragments.0.source_turn_id=''``, followed by a full
        retry that then succeeded in 18s. ``_stamped_for_turn`` passes
        ``turn.turn_id`` into ``stamp_fragment_identity`` unconditionally, so the
        rejected value was one this pipeline was about to overwrite with a value
        it already held.

        That closes the class rather than a third instance of it.
        ``stamp_fragment_identity`` overwrites nine fields; exactly two of them
        are also rejected-when-blank by ``MemoryFragment``, and both are now
        taken back here. ``test_steward_identity_is_not_the_models_job`` asserts
        that intersection stays covered, so a tenth stamped field that is also
        validated cannot quietly reintroduce this.
        """

        try:
            data = json.loads(_strip_json_fence(raw))
        except json.JSONDecodeError as exc:
            msg = f"invalid LLM steward output: {exc}"
            raise StewardOutputError(msg) from exc

        space_id = ""
        if context is not None:
            space_id = str(
                getattr(context, "memory_space_id", "")
                or getattr(context, "memory_realm_id", "")
                or ""
            ).strip()
        turn_id = str(source_turn_id or "").strip()
        if isinstance(data, dict) and (space_id or turn_id):
            for fragment in data.get("fragments") or []:
                if not isinstance(fragment, dict):
                    continue
                if space_id:
                    fragment["memory_space_id"] = space_id
                if turn_id:
                    fragment["source_turn_id"] = turn_id

        for dropped in _drop_unusable_extensions(data):
            log.warning("steward_extension_dropped", field=dropped)

        try:
            return StewardDecision.model_validate(data)
        except ValidationError as exc:
            msg = f"invalid LLM steward output: {exc}"
            raise StewardOutputError(msg) from exc


def _drop_unusable_extensions(data: Any) -> list[str]:
    """Remove extension entries the schema cannot hold, and name what went.

    ``extensions`` is a namespace → dict map. A model reading it as a free-form
    annotation slot writes ``{"note": "原话中「她」指向…"}``, which is a string
    where a dict belongs — and because validation is all-or-nothing, that one
    annotation discarded every fragment and every triple the model had extracted
    for the turn. Observed once in 90 turns, so it must not turn an otherwise
    usable extraction into a retry.

    This is the second time the same lesson has been learned in this function —
    the docstring above it records a model omitting ``memory_space_id`` and
    failing the whole decision over a field we were about to overwrite anyway.
    Both are the same shape: a field that is not the substance of a memory
    deciding whether the memory exists.

    Dropping is right *because* extensions are annotation. Content, wing,
    importance and memory_type are the memory itself, and repairing those would
    be inventing one; they are still allowed to fail the decision.
    """

    dropped: list[str] = []
    if not isinstance(data, dict):
        return dropped

    for index, fragment in enumerate(data.get("fragments") or []):
        if not isinstance(fragment, dict) or "extensions" not in fragment:
            continue
        extensions = fragment["extensions"]
        if not isinstance(extensions, dict):
            fragment.pop("extensions")
            dropped.append(f"fragments[{index}].extensions")
            continue
        for namespace in list(extensions):
            payload = extensions[namespace]
            if not is_usable_extension(namespace, payload):
                extensions.pop(namespace)
                dropped.append(f"fragments[{index}].extensions.{namespace}")
    return dropped


def _validate_user_evidence(decision: StewardDecision, user_text: str) -> None:
    """Require every durable action to cite verbatim user evidence.

    The extractor may normalize a fact into a fragment or triple, but it may not
    use the assistant reply or an inferred detail as authority. Directly-created
    domain objects keep backwards-compatible defaults; this boundary applies to
    untrusted model output before it reaches the ledger.
    """

    actions = [
        *decision.fragments,
        *decision.triples,
        *decision.invalidations,
        *decision.privacy_actions,
    ]
    for index, action in enumerate(actions):
        quote = str(getattr(action, "evidence_quote", "") or "").strip()
        if not quote:
            raise StewardOutputError(f"steward action {index} is missing a user evidence_quote")
        if quote not in user_text:
            raise StewardOutputError(
                f"steward action {index} evidence_quote is not verbatim user text"
            )


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
