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


async def _run_case(sample: dict, proposer) -> dict:
    from eidolon.memory.domain.commitment_shadow import CommitmentShadowInput

    shadow_input = CommitmentShadowInput.model_validate(sample["input"])
    expected = sample["expect"]
    started = time.perf_counter()
    try:
        candidate = await proposer.propose(shadow_input)
    except Exception as exc:  # noqa: BLE001 - report every model/schema failure
        return {
            "name": sample["name"],
            "expected_operation": expected["operation"],
            "expected_target_id": expected.get("target_id"),
            "expected_action": expected.get("action"),
            "actual_operation": None,
            "actual_target_id": None,
            "actual_action": None,
            "actual_confidence": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }
    top_target = (
        candidate.target_candidates[0].commitment_id
        if candidate.target_candidates
        else None
    )
    return {
        "name": sample["name"],
        "expected_operation": expected["operation"],
        "expected_target_id": expected.get("target_id"),
        "expected_action": expected.get("action"),
        "actual_operation": candidate.operation,
        "actual_target_id": top_target,
        "actual_action": candidate.action,
        "actual_confidence": candidate.confidence,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "candidate": candidate.model_dump(mode="json"),
        "error": None,
    }


async def _amain(args: argparse.Namespace) -> int:
    from eidolon.memory.application.commitment_shadow import (
        LiteLLMCommitmentShadowProposer,
    )
    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.domain.commitment_shadow import score_commitment_shadow

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
    settings = get_memory_settings()
    proposer = LiteLLMCommitmentShadowProposer(settings.llm)
    print(
        f"[commitment-shadow] samples={len(samples)} "
        f"version={proposer.extraction_version} model={settings.llm.model}"
    )
    observations = []
    for sample in samples:
        row = await _run_case(sample, proposer)
        observations.append(row)
        print(
            f"  {row['name']:<34s} expected={row['expected_operation']:<10s} "
            f"actual={str(row['actual_operation']):<10s} "
            f"target={str(row['actual_target_id']):<24s} "
            f"{row['elapsed_ms']}ms"
            + (f" error={row['error']}" if row["error"] else "")
        )
    aggregate = score_commitment_shadow(observations)
    report = {
        "schema_version": "eidolon_memory.commitment_shadow_eval.v1",
        "extractor_version": proposer.extraction_version,
        "model": settings.llm.model,
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
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
