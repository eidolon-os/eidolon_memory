from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidolon.memory.application.commitment_shadow import (
    LiteLLMCommitmentShadowProposer,
)
from eidolon.memory.config.memory_settings import LlmConfig
from eidolon.memory.domain.commitment_shadow import (
    CommitmentShadowCandidate,
    CommitmentShadowInput,
    CommitmentShadowOutputError,
    CommitmentShadowTarget,
    CommitmentTargetCandidate,
    score_commitment_shadow,
    validate_shadow_candidate,
)


def _target(commitment_id: str = "commitment-1") -> CommitmentShadowTarget:
    return CommitmentShadowTarget(
        commitment_id=commitment_id,
        promisor="owner",
        predicate="promised",
        action="陪妈妈去医院复查",
        status="confirmed",
    )


def _input(*targets: CommitmentShadowTarget) -> CommitmentShadowInput:
    return CommitmentShadowInput(
        memory_space_id="realm-1",
        source_turn_id="turn-1",
        user_text="今天已经陪妈妈去医院复查了。",
        assistant_text="辛苦了。",
        active_commitments=list(targets),
    )


def test_shadow_candidate_accepts_known_active_target_and_verbatim_evidence() -> None:
    candidate = CommitmentShadowCandidate(
        operation="fulfil",
        target_candidates=[
            CommitmentTargetCandidate(
                commitment_id="commitment-1",
                confidence=0.98,
            )
        ],
        confidence=0.97,
        evidence_quote="已经陪妈妈去医院复查了",
    )

    assert validate_shadow_candidate(_input(_target()), candidate) is candidate


def test_shadow_candidate_rejects_unknown_target_and_non_verbatim_evidence() -> None:
    unknown = CommitmentShadowCandidate(
        operation="cancel",
        target_candidates=[
            CommitmentTargetCandidate(
                commitment_id="commitment-other",
                confidence=0.8,
            )
        ],
        confidence=0.8,
        evidence_quote="今天已经陪妈妈去医院复查了",
    )
    with pytest.raises(CommitmentShadowOutputError, match="outside the supplied"):
        validate_shadow_candidate(_input(_target()), unknown)

    unsupported = unknown.model_copy(
        update={
            "target_candidates": [
                CommitmentTargetCandidate(
                    commitment_id="commitment-1",
                    confidence=0.8,
                )
            ],
            "evidence_quote": "模型自己补写的证据",
        }
    )
    with pytest.raises(CommitmentShadowOutputError, match="verbatim"):
        validate_shadow_candidate(_input(_target()), unsupported)


def test_shadow_candidate_evidence_is_case_sensitive_exact_text() -> None:
    shadow_input = CommitmentShadowInput(
        memory_space_id="realm-1",
        source_turn_id="turn-english",
        user_text="I promise to call Mum tomorrow.",
    )
    candidate = CommitmentShadowCandidate(
        operation="create",
        promisor="owner",
        predicate="promised",
        action="call Mum tomorrow",
        confidence=0.9,
        evidence_quote="i promise to call Mum tomorrow",
    )

    with pytest.raises(CommitmentShadowOutputError, match="verbatim"):
        validate_shadow_candidate(shadow_input, candidate)


@pytest.mark.parametrize("operation", ["supplement", "fulfil", "cancel", "supersede"])
def test_targeted_shadow_operations_require_candidates(operation: str) -> None:
    with pytest.raises(ValueError, match="requires at least one target"):
        CommitmentShadowCandidate(
            operation=operation,
            confidence=0.8,
            evidence_quote="证据",
        )


def test_shadow_candidate_rejects_unknown_output_fields() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        CommitmentShadowCandidate.model_validate(
            {
                "operation": "none",
                "confidence": 0.7,
                "evidence_quote": "",
                "authoritative_action": "write_now",
            }
        )


def test_shadow_score_exposes_false_positive_and_schema_gates() -> None:
    observations = [
        {
            "expected_operation": "create",
            "actual_operation": "create",
            "expected_target_id": None,
            "actual_target_id": None,
            "expected_action": "周六陪妈妈去医院",
            "actual_action": "周六陪妈妈去医院",
            "actual_confidence": 0.95,
            "error": None,
        },
        {
            "expected_operation": "fulfil",
            "actual_operation": "fulfil",
            "expected_target_id": "commitment-1",
            "actual_target_id": "commitment-1",
            "expected_action": "陪妈妈去医院复查",
            "actual_action": "陪妈妈去医院复查",
            "actual_confidence": 0.95,
            "error": None,
        },
        {
            "expected_operation": "none",
            "actual_operation": "create",
            "expected_target_id": None,
            "actual_target_id": None,
            "expected_action": None,
            "actual_action": "去冰岛",
            "actual_confidence": 0.9,
            "error": None,
        },
        {
            "expected_operation": "cancel",
            "actual_operation": None,
            "expected_target_id": "commitment-2",
            "actual_target_id": None,
            "expected_action": "另一件事",
            "actual_action": None,
            "actual_confidence": None,
            "error": "schema invalid",
        },
    ]

    result = score_commitment_shadow(observations)

    assert result["counts"]["schema_failures"] == 1
    assert result["metrics"]["operation_accuracy"] == pytest.approx(
        2 / 3,
        abs=0.0001,
    )
    assert result["metrics"]["action_exact_accuracy"] == 1.0
    assert result["metrics"]["target_top1_accuracy"] == 1.0
    assert result["metrics"]["none_false_positive_rate"] == 1.0
    assert result["gates"]["target_case_coverage"] is True
    assert result["gates"]["none_case_coverage"] is True
    assert result["counts"]["high_confidence_errors"] == 1
    assert result["gates"]["overall_pass"] is False


def test_shadow_score_separates_provider_schema_and_policy_failures() -> None:
    observations = [
        {"error": "empty", "error_type": "provider_empty"},
        {"error": "invalid schema", "error_type": "schema_invalid"},
        {"error": "unknown target", "error_type": "policy_rejected"},
    ]

    result = score_commitment_shadow(observations)

    assert result["counts"]["provider_failures"] == 1
    assert result["counts"]["schema_failures"] == 1
    assert result["counts"]["policy_rejections"] == 1
    assert result["metrics"]["provider_failure_rate"] == pytest.approx(
        1 / 3, abs=0.0001
    )
    assert result["metrics"]["schema_failure_rate"] == pytest.approx(
        1 / 3, abs=0.0001
    )
    assert result["metrics"]["policy_rejection_rate"] == pytest.approx(
        1 / 3, abs=0.0001
    )
    assert result["gates"]["overall_pass"] is False


@pytest.mark.asyncio
async def test_litellm_shadow_proposer_uses_json_mode_and_never_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "operation": "fulfil",
                                "promisor": "owner",
                                "predicate": "promised",
                                "action": "陪妈妈去医院复查",
                                "target_candidates": [
                                    {
                                        "commitment_id": "commitment-1",
                                        "confidence": 0.98,
                                        "reason": "动作和对象一致",
                                    }
                                ],
                                "confidence": 0.97,
                                "evidence_quote": "已经陪妈妈去医院复查了",
                                "reason": "用户明确表示已完成",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=fake_acompletion),
    )
    proposer = LiteLLMCommitmentShadowProposer(
        LlmConfig(model="openai/test", base_url="http://model.invalid/v1")
    )

    candidate = await proposer.propose(_input(_target()))

    assert candidate.operation == "fulfil"
    assert candidate.target_candidates[0].commitment_id == "commitment-1"
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 1200
    assert captured["response_format"] == {"type": "json_object"}
    prompt_input = json.loads(captured["messages"][1]["content"])
    assert prompt_input["memory_space_id"] == "realm-1"
    assert len(prompt_input["active_commitments"]) == 1
    assert not hasattr(proposer, "apply")
    assert not hasattr(proposer, "writer")


@pytest.mark.asyncio
async def test_litellm_shadow_proposer_reports_empty_provider_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**kwargs):
        return {"choices": [{"message": {"content": None}}]}

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(acompletion=fake_acompletion),
    )
    proposer = LiteLLMCommitmentShadowProposer(LlmConfig(model="openai/test"))

    with pytest.raises(CommitmentShadowOutputError) as raised:
        await proposer.propose(_input())

    assert raised.value.failure_type == "provider_empty"


def test_fixed_shadow_dataset_is_bounded_and_covers_lifecycle() -> None:
    path = Path(__file__).with_name("eval_commitment_shadow.jsonl")
    samples = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    operations = set()
    for sample in samples:
        parsed = CommitmentShadowInput.model_validate(sample["input"])
        assert len(parsed.active_commitments) <= 10
        operations.add(sample["expect"]["operation"])
    assert operations == {
        "none",
        "create",
        "supplement",
        "fulfil",
        "cancel",
        "supersede",
    }
