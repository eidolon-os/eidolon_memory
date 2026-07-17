"""Operation-level scoring contracts for the live steward evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.benchmark.eval_steward_prompt import (
    _aggregate,
    _error_rates,
    _run_one,
    _triple_diff,
    _validate_samples,
)


def _result(
    *,
    name: str,
    category: str,
    triples: tuple[int, int, int] = (0, 0, 0),
    invalidations: tuple[int, int, int] = (0, 0, 0),
    should_write_ok: bool = True,
    privacy_expected: str | None = None,
    privacy_ok: bool = True,
) -> dict:
    return {
        "name": name,
        "category": category,
        "should_write_expected": True,
        "should_write_actual": should_write_ok,
        "should_write_ok": should_write_ok,
        "triples": dict(zip(("tp", "fp", "fn"), triples, strict=True)),
        "invalidations": dict(
            zip(("tp", "fp", "fn"), invalidations, strict=True)
        ),
        "mentions": {"tp": 0, "fp": 0, "fn": 0},
        "privacy_expected": privacy_expected,
        "privacy_actual": None,
        "privacy_ok": privacy_ok,
    }


def test_error_rates_separate_hallucination_from_omission() -> None:
    hallucination, omission = _error_rates(tp=2, fp=1, fn=2)

    assert hallucination == 1 / 3
    assert omission == 0.5


def test_triple_diff_preserves_actual_values_for_manual_audit() -> None:
    diff = _triple_diff(
        [{"subject": "self", "predicate": "lives_in", "object": "江城"}],
        [{"subject": "self", "predicate": "works_at", "object": "江城"}],
    )

    assert diff["false_negative"] == [
        {"subject": "self", "predicate": "lives_in", "object": "江城"}
    ]
    assert diff["false_positive"] == [
        {"subject": "self", "predicate": "works_at", "object": "江城"}
    ]


def test_aggregate_reports_extraction_update_and_write_failures() -> None:
    aggregate = _aggregate(
        [
            _result(
                name="extract",
                category="extraction",
                triples=(1, 1, 1),
                should_write_ok=False,
            ),
            _result(
                name="update",
                category="update",
                triples=(1, 0, 0),
                invalidations=(1, 1, 1),
            ),
        ]
    )
    gates = aggregate["gates"]

    assert gates["triple_hallucination_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert gates["triple_omission_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert gates["update_hallucination_rate"] == 0.5
    assert gates["update_omission_rate"] == 0.5
    assert gates["should_write_accuracy"] == 0.5
    assert gates["pass_should_write"] is False
    assert aggregate["categories"]["extraction"]["samples"] == 1
    assert aggregate["categories"]["update"]["samples"] == 1


def test_unexpected_privacy_action_is_an_error_not_a_free_pass() -> None:
    aggregate = _aggregate(
        [
            _result(
                name="false_privacy",
                category="privacy",
                privacy_expected=None,
                privacy_ok=False,
            )
        ]
    )

    assert aggregate["gates"]["privacy_misses"] == 0
    assert aggregate["gates"]["privacy_errors"] == 1
    assert aggregate["gates"]["pass_privacy"] is False


def test_zero_evaluated_samples_cannot_report_an_overall_pass() -> None:
    gates = _aggregate([])["gates"]

    assert gates["evaluated_samples"] == 0
    assert gates["pass_coverage"] is False
    assert gates["overall_pass"] is False


def test_partial_live_run_fails_coverage_gate() -> None:
    gates = _aggregate(
        [_result(name="one", category="extraction")], requested_count=2
    )["gates"]

    assert gates["requested_samples"] == 2
    assert gates["evaluated_samples"] == 1
    assert gates["failed_samples"] == 1
    assert gates["pass_coverage"] is False
    assert gates["overall_pass"] is False


def test_example_dataset_is_categorized_and_uses_no_placeholder_objects() -> None:
    path = Path(__file__).with_name("eval_steward_dataset.example.jsonl")
    samples = [json.loads(line) for line in path.read_text().splitlines() if line]

    _validate_samples(samples)

    assert len(samples) >= 24
    assert {sample["category"] for sample in samples} >= {
        "information_extraction",
        "knowledge_update",
        "semantic_grounding",
        "no_write",
        "privacy",
    }


def test_dataset_validation_rejects_unknown_as_if_it_were_evidence() -> None:
    with pytest.raises(ValueError, match="placeholder objects"):
        _validate_samples(
            [
                {
                    "name": "placeholder",
                    "category": "semantic_grounding",
                    "user_text": "the medicine was not named",
                    "expect": {
                        "should_write": True,
                        "triples": [
                            {
                                "subject": "self",
                                "predicate": "takes_medication",
                                "object": "unknown",
                            }
                        ],
                        "invalidations": [],
                    },
                }
            ]
        )


@pytest.mark.asyncio
async def test_live_eval_builds_current_actor_context_contract() -> None:
    class _Steward:
        turn = None

        async def decide(self, turn):
            self.turn = turn
            return SimpleNamespace(
                should_write=False,
                triples=[],
                invalidations=[],
                mentions=[],
                privacy_actions=[],
            )

    steward = _Steward()
    result = await _run_one(
        {
            "name": "no-write",
            "category": "no_write",
            "user_text": "hello",
            "assistant_text": "hi",
            "expect": {
                "should_write": False,
                "triples": [],
                "invalidations": [],
                "privacy_action": None,
            },
        },
        steward,
        "eval",
    )

    assert steward.turn.context.owner_id == "eval"
    assert steward.turn.context.memory_realm_id == "r:eval:steward-eval"
    assert steward.turn.context.memory_space_id == "r:eval:steward-eval"
    assert result["should_write_ok"] is True
