#!/usr/bin/env python3
"""Evaluate shadow-only Commitment interpretation against a fixed JSONL set.

This command never starts a Realm worker, publishes NATS commands, or receives
a CommitmentWriter. It only calls the configured LLM and writes a local report.

Run explicitly:

    EIDOLON_MEMORY_RUN_LIVE=1 .venv/bin/python \
      scripts/benchmark/eval_commitment_shadow.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def _run_case(sample: dict, proposer, *, run_index: int = 1) -> dict:
    from eidolon.memory.domain.commitment_shadow import (
        CommitmentShadowInput,
        CommitmentShadowOutputError,
    )

    shadow_input = CommitmentShadowInput.model_validate(sample["input"])
    expected = sample["expect"]
    started = time.perf_counter()
    try:
        candidate = await proposer.propose(shadow_input)
    except Exception as exc:  # noqa: BLE001 - report every model/schema failure
        error_type = (
            exc.failure_type
            if isinstance(exc, CommitmentShadowOutputError)
            else "provider_error"
        )
        return {
            "name": sample["name"],
            "run_index": run_index,
            "expected_operation": expected["operation"],
            "expected_target_id": expected.get("target_id"),
            "expected_action": expected.get("action"),
            "actual_operation": None,
            "actual_target_id": None,
            "actual_action": None,
            "actual_confidence": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error_type": error_type,
            "error": f"{type(exc).__name__}: {exc}",
        }
    top_target = (
        candidate.target_candidates[0].commitment_id
        if candidate.target_candidates
        else None
    )
    return {
        "name": sample["name"],
        "run_index": run_index,
        "expected_operation": expected["operation"],
        "expected_target_id": expected.get("target_id"),
        "expected_action": expected.get("action"),
        "actual_operation": candidate.operation,
        "actual_target_id": top_target,
        "actual_action": candidate.action,
        "actual_confidence": candidate.confidence,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "candidate": candidate.model_dump(mode="json"),
        "error_type": None,
        "error": None,
    }


async def _amain(args: argparse.Namespace) -> int:
    from eidolon.memory.application.commitment_shadow import (
        LiteLLMCommitmentShadowProposer,
    )
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.domain.commitment_shadow import score_commitment_shadow

    if not 1 <= args.runs <= 100:
        print("[commitment-shadow] --runs must be between 1 and 100")
        return 2
    if args.limit < 0:
        print("[commitment-shadow] --limit must be zero or greater")
        return 2
    if os.environ.get("EIDOLON_MEMORY_RUN_LIVE") != "1":
        print("[commitment-shadow] set EIDOLON_MEMORY_RUN_LIVE=1 to call the LLM")
        return 2
    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        print(f"[commitment-shadow] dataset missing: {dataset_path}")
        return 2
    samples = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        samples = samples[: args.limit]
    settings = get_memory_settings()
    llm = (
        settings.llm.model_copy(update={"model": args.model})
        if args.model
        else settings.llm
    )
    proposer = LiteLLMCommitmentShadowProposer(llm, thinking=args.thinking)
    print(
        f"[commitment-shadow] samples={len(samples)} runs={args.runs} "
        f"version={proposer.extraction_version} model={llm.model}"
    )
    observations = []
    for run_index in range(1, args.runs + 1):
        for sample in samples:
            row = await _run_case(sample, proposer, run_index=run_index)
            observations.append(row)
            print(
                f"  run={run_index:<3d} {row['name']:<34s} "
                f"expected={row['expected_operation']:<10s} "
                f"actual={str(row['actual_operation']):<10s} "
                f"target={str(row['actual_target_id']):<24s} "
                f"{row['elapsed_ms']}ms"
                + (f" error={row['error']}" if row["error"] else "")
            )
    aggregate = score_commitment_shadow(observations)
    report = {
        "schema_version": "eidolon_memory.commitment_shadow_eval.v1",
        "extractor_version": proposer.extraction_version,
        "model": llm.model,
        "thinking": args.thinking,
        "runs_per_sample": args.runs,
        "attempts_per_case": 1,
        "retry_policy": "none",
        "authoritative_writes": 0,
        "dataset": str(dataset_path),
        "observations": observations,
        "aggregate": aggregate,
    }
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[commitment-shadow] wrote {out}")
    return 0 if aggregate["gates"]["overall_pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default="tests/memory/eval_commitment_shadow.jsonl",
    )
    parser.add_argument(
        "--out",
        default="reports/commitment_shadow_eval.json",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="repeat the fixed set 1-100 times without retrying failed calls",
    )
    parser.add_argument(
        "--model",
        default="",
        help="override llm.model for an explicit offline A/B run",
    )
    parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default="enabled",
        help="set the provider thinking mode explicitly",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="evaluate only the first N fixed cases; zero means all",
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
