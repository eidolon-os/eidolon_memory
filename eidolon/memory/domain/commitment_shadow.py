"""Shadow-only Commitment candidates and offline evaluation metrics.

These models deliberately have no conversion to ``MemoryIntent`` and no writer
port. They exist to measure LLM interpretation quality before any product
authority is considered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from eidolon.memory.support.model_base import BaseEidolonModel

CommitmentShadowOperation = Literal[
    "none",
    "create",
    "supplement",
    "fulfil",
    "cancel",
    "supersede",
]


class CommitmentShadowOutputError(ValueError):
    """A shadow result violates the bounded input or evidence contract."""

    def __init__(self, message: str, *, failure_type: str = "policy_rejected") -> None:
        super().__init__(message)
        self.failure_type = failure_type


class CommitmentShadowTarget(BaseEidolonModel):
    """Small active-only target snapshot supplied by a Realm-bound reader."""

    model_config = {"extra": "forbid"}

    commitment_id: str = Field(min_length=1)
    promisor: str = Field(min_length=1)
    predicate: Literal["promised", "committed_to", "planned_to"]
    action: str = Field(min_length=1)
    status: Literal["proposed", "confirmed"]
    beneficiaries: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    condition: str | None = None
    due_at: str | None = None


class CommitmentShadowInput(BaseEidolonModel):
    """One offline turn plus a deterministic bounded active candidate set."""

    model_config = {"extra": "forbid"}

    memory_space_id: str = Field(min_length=1)
    source_turn_id: str = Field(min_length=1)
    user_text: str = ""
    assistant_text: str = ""
    active_commitments: list[CommitmentShadowTarget] = Field(
        default_factory=list,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_transcript_and_targets(self) -> CommitmentShadowInput:
        if not self.user_text.strip() and not self.assistant_text.strip():
            raise ValueError("shadow input requires user or assistant text")
        ids = [item.commitment_id for item in self.active_commitments]
        if len(ids) != len(set(ids)):
            raise ValueError("active commitment ids must be unique")
        return self


class CommitmentTargetCandidate(BaseEidolonModel):
    model_config = {"extra": "forbid"}

    commitment_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class CommitmentShadowCandidate(BaseEidolonModel):
    """One non-authoritative LLM suggestion for offline/shadow scoring."""

    model_config = {"extra": "forbid"}

    operation: CommitmentShadowOperation
    promisor: str = ""
    predicate: Literal["promised", "committed_to", "planned_to"] | None = None
    action: str = ""
    beneficiaries: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    condition: str | None = None
    due_at: str | None = None
    target_candidates: list[CommitmentTargetCandidate] = Field(
        default_factory=list,
        max_length=5,
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> CommitmentShadowCandidate:
        ids = [item.commitment_id for item in self.target_candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("target candidate ids must be unique")
        if self.operation in {"none", "create"} and ids:
            raise ValueError(f"{self.operation} must not name existing targets")
        if self.operation in {"supplement", "fulfil", "cancel", "supersede"} and not ids:
            raise ValueError(f"{self.operation} requires at least one target candidate")
        if self.operation == "create" and (
            not self.promisor.strip()
            or self.predicate is None
            or not self.action.strip()
        ):
            raise ValueError("create requires promisor, predicate and action")
        return self


def validate_shadow_candidate(
    shadow_input: CommitmentShadowInput,
    candidate: CommitmentShadowCandidate,
) -> CommitmentShadowCandidate:
    """Fail closed on cross-set target ids and unsupported evidence claims."""
    allowed = {
        item.commitment_id for item in shadow_input.active_commitments
    }
    selected = {
        item.commitment_id for item in candidate.target_candidates
    }
    unknown = sorted(selected - allowed)
    if unknown:
        raise CommitmentShadowOutputError(
            f"target candidates are outside the supplied active set: {unknown}"
        )
    if candidate.operation != "none":
        quote = candidate.evidence_quote.strip()
        transcript = f"{shadow_input.user_text}\n{shadow_input.assistant_text}"
        if not quote or quote not in transcript:
            raise CommitmentShadowOutputError(
                "non-none candidate requires a verbatim transcript evidence quote"
            )
    return candidate


def score_commitment_shadow(observations: list[dict]) -> dict:
    """Aggregate fixed-set metrics and conservative offline review gates."""
    total = len(observations)
    valid = [row for row in observations if not row.get("error")]
    failed = [row for row in observations if row.get("error")]
    schema_failures = sum(
        (row.get("error_type") or "schema_invalid") == "schema_invalid"
        for row in failed
    )
    provider_failures = sum(
        row.get("error_type") in {"provider_error", "provider_empty"}
        for row in failed
    )
    policy_rejections = sum(
        row.get("error_type") == "policy_rejected" for row in failed
    )
    elapsed_ms = sorted(
        float(row["elapsed_ms"])
        for row in observations
        if row.get("elapsed_ms") is not None
    )
    named_groups: dict[str, list[dict]] = {}
    for row in observations:
        if name := str(row.get("name") or "").strip():
            named_groups.setdefault(name, []).append(row)
    runs_per_case = [len(rows) for rows in named_groups.values()]
    repeat_case_coverage = bool(runs_per_case) and min(runs_per_case) >= 2
    consistent_cases = sum(
        len({_outcome_key(row) for row in rows}) == 1
        for rows in named_groups.values()
    )
    all_runs_correct_cases = sum(
        all(not row.get("error") and _observation_correct(row) for row in rows)
        for rows in named_groups.values()
    )
    case_consistency_rate = _ratio(consistent_cases, len(named_groups))
    all_runs_correct_rate = _ratio(all_runs_correct_cases, len(named_groups))
    operation_correct = sum(
        row.get("actual_operation") == row.get("expected_operation")
        for row in valid
    )
    action_rows = [row for row in valid if row.get("expected_action")]
    action_correct = sum(
        _normalize(str(row.get("actual_action") or ""))
        == _normalize(str(row.get("expected_action") or ""))
        for row in action_rows
    )
    target_rows = [
        row
        for row in valid
        if row.get("expected_target_id")
    ]
    target_correct = sum(
        row.get("actual_target_id") == row.get("expected_target_id")
        for row in target_rows
    )
    none_rows = [
        row
        for row in valid
        if row.get("expected_operation") == "none"
    ]
    none_false_positives = sum(
        row.get("actual_operation") != "none" for row in none_rows
    )
    high_confidence_errors = sum(
        float(row.get("actual_confidence") or 0.0) >= 0.8
        and not _observation_correct(row)
        for row in valid
    )

    operation_accuracy = _ratio(operation_correct, len(valid))
    action_exact_accuracy = _ratio(action_correct, len(action_rows))
    target_top1_accuracy = _ratio(target_correct, len(target_rows))
    none_false_positive_rate = (
        _ratio(none_false_positives, len(none_rows)) if none_rows else 0.0
    )
    schema_failure_rate = _ratio(schema_failures, total)
    provider_failure_rate = _ratio(provider_failures, total)
    policy_rejection_rate = _ratio(policy_rejections, total)
    gates = {
        "operation_accuracy": operation_accuracy >= 0.90,
        "action_exact_accuracy": action_exact_accuracy >= 0.80,
        "target_top1_accuracy": target_top1_accuracy >= 0.90,
        "none_false_positive_rate": none_false_positive_rate <= 0.05,
        "schema_failure_rate": schema_failure_rate == 0.0,
        "provider_failure_rate": provider_failure_rate == 0.0,
        "policy_rejection_rate": policy_rejection_rate == 0.0,
        "repeat_case_coverage": repeat_case_coverage,
        "case_consistency_rate": case_consistency_rate >= 0.95,
        "high_confidence_errors": high_confidence_errors == 0,
        "action_case_coverage": bool(action_rows),
        "target_case_coverage": bool(target_rows),
        "none_case_coverage": bool(none_rows),
    }
    gates["overall_pass"] = total > 0 and all(gates.values())
    return {
        "counts": {
            "total": total,
            "valid": len(valid),
            "schema_failures": schema_failures,
            "provider_failures": provider_failures,
            "policy_rejections": policy_rejections,
            "operation_correct": operation_correct,
            "action_cases": len(action_rows),
            "action_correct": action_correct,
            "target_cases": len(target_rows),
            "target_correct": target_correct,
            "none_cases": len(none_rows),
            "none_false_positives": none_false_positives,
            "high_confidence_errors": high_confidence_errors,
        },
        "metrics": {
            "operation_accuracy": operation_accuracy,
            "action_exact_accuracy": action_exact_accuracy,
            "target_top1_accuracy": target_top1_accuracy,
            "none_false_positive_rate": none_false_positive_rate,
            "schema_failure_rate": schema_failure_rate,
            "provider_failure_rate": provider_failure_rate,
            "policy_rejection_rate": policy_rejection_rate,
        },
        "latency_ms": {
            "samples": len(elapsed_ms),
            "p50": _percentile(elapsed_ms, 50),
            "p95": _percentile(elapsed_ms, 95),
            "p99": _percentile(elapsed_ms, 99),
        },
        "stability": {
            "cases": len(named_groups),
            "min_runs_per_case": min(runs_per_case) if runs_per_case else 0,
            "max_runs_per_case": max(runs_per_case) if runs_per_case else 0,
            "consistent_cases": consistent_cases,
            "case_consistency_rate": case_consistency_rate,
            "all_runs_correct_cases": all_runs_correct_cases,
            "all_runs_correct_rate": all_runs_correct_rate,
        },
        "gates": gates,
    }


def _normalize(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _observation_correct(row: dict) -> bool:
    if row.get("actual_operation") != row.get("expected_operation"):
        return False
    expected_target = row.get("expected_target_id")
    if expected_target and row.get("actual_target_id") != expected_target:
        return False
    expected_action = row.get("expected_action")
    if expected_action and _normalize(str(row.get("actual_action") or "")) != _normalize(
        str(expected_action)
    ):
        return False
    return True


def _outcome_key(row: dict) -> tuple[str, str, str, str]:
    return (
        str(row.get("error_type") or ""),
        str(row.get("actual_operation") or ""),
        str(row.get("actual_target_id") or ""),
        _normalize(str(row.get("actual_action") or "")),
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 4)


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    rank = max(1, (len(values) * percentile + 99) // 100)
    return round(values[min(rank, len(values)) - 1], 1)


__all__ = [
    "CommitmentShadowCandidate",
    "CommitmentShadowInput",
    "CommitmentShadowOperation",
    "CommitmentShadowOutputError",
    "CommitmentShadowTarget",
    "CommitmentTargetCandidate",
    "score_commitment_shadow",
    "validate_shadow_candidate",
]
