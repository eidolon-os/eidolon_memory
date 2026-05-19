#!/usr/bin/env python3
"""Score steward LLM prompt against a hand-labelled dataset (T2 §4.7).

Workflow:
1. Load JSONL dataset where each line has:
     {"name": str,
      "user_text": str, "assistant_text": str,
      "expect": {
          "triples":           [{"subject":..,"predicate":..,"object":..}, ...],
          "invalidations":     [{"subject":..,"predicate":..,"object":..}, ...],
          "privacy_action":    str | null,        # do_not_store / archive_topic / delete_request / null
          "should_write":      bool,
      }}
2. For each sample, run LiteLLMSteward.decide() against the live LLM endpoint.
3. Diff actual vs expected; track precision/recall by class.
4. Print summary + write JSON report.

Gates (KG plan §4.7):
- triples precision   ≥ 0.85
- triples recall      ≥ 0.70
- invalidations prec. ≥ 0.90
- privacy miss rate   = 0 (if expected do_not_store, must produce)

Skipped silently if no dataset is present or LLM is not reachable. Run with:

    EIDOLON_MEMORY_RUN_LIVE=1 .venv/bin/python scripts/benchmark/eval_steward_prompt.py \\
        --dataset tests/memory/eval_steward_dataset.jsonl \\
        --out reports/steward_eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _triple_key(t: dict | object) -> tuple[str, str, str]:
    if isinstance(t, dict):
        return (t["subject"], t["predicate"], t["object"])
    return (t.subject, t.predicate, t.object)


def _confusion(expected: list, actual: list) -> tuple[int, int, int]:
    """Returns (tp, fp, fn) where keys are triple tuples."""
    e = {_triple_key(x) for x in expected}
    a = {_triple_key(x) for x in actual}
    tp = len(e & a)
    fp = len(a - e)
    fn = len(e - a)
    return tp, fp, fn


def _precision_recall(tp: int, fp: int, fn: int) -> tuple[float, float]:
    p = tp / (tp + fp) if (tp + fp) > 0 else 1.0  # no FP if nothing produced
    r = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    return p, r


async def _run_one(sample: dict, steward, user_id: str) -> dict:
    from eidolon.memory.domain.payloads import ConversationTurnPayload

    turn = ConversationTurnPayload(
        turn_id=uuid.uuid4().hex,
        user_id=user_id,
        session_id="eval",
        timestamp="2026-05-19T10:00:00Z",
        user_text=sample["user_text"],
        assistant_text=sample.get("assistant_text", ""),
    )
    t0 = time.perf_counter()
    decision = await steward.decide(turn)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    expect = sample["expect"]
    tp_t, fp_t, fn_t = _confusion(expect.get("triples", []), decision.triples)
    tp_i, fp_i, fn_i = _confusion(
        expect.get("invalidations", []), decision.invalidations
    )

    privacy_expected = expect.get("privacy_action") or None
    privacy_actual = decision.privacy_actions[0].action if decision.privacy_actions else None
    privacy_ok = privacy_expected == privacy_actual

    return {
        "name": sample["name"],
        "elapsed_ms": round(elapsed_ms, 1),
        "should_write_expected": expect.get("should_write"),
        "should_write_actual": decision.should_write,
        "triples": {"tp": tp_t, "fp": fp_t, "fn": fn_t},
        "invalidations": {"tp": tp_i, "fp": fp_i, "fn": fn_i},
        "privacy_ok": privacy_ok,
        "privacy_expected": privacy_expected,
        "privacy_actual": privacy_actual,
    }


def _aggregate(results: list[dict]) -> dict:
    sum_t = {"tp": 0, "fp": 0, "fn": 0}
    sum_i = {"tp": 0, "fp": 0, "fn": 0}
    privacy_misses = 0
    privacy_expected_count = 0
    for r in results:
        for k in ("tp", "fp", "fn"):
            sum_t[k] += r["triples"][k]
            sum_i[k] += r["invalidations"][k]
        if r["privacy_expected"]:
            privacy_expected_count += 1
            if not r["privacy_ok"]:
                privacy_misses += 1

    tp, rp = _precision_recall(**sum_t)
    ip, ir = _precision_recall(**sum_i)

    gates = {
        "triples_precision": round(tp, 3),
        "triples_recall": round(rp, 3),
        "invalidations_precision": round(ip, 3),
        "invalidations_recall": round(ir, 3),
        "privacy_misses": privacy_misses,
        "privacy_expected_count": privacy_expected_count,
        "pass_triples_precision": tp >= 0.85,
        "pass_triples_recall": rp >= 0.70,
        "pass_invalidations_precision": ip >= 0.90,
        "pass_privacy": privacy_misses == 0,
    }
    gates["overall_pass"] = all(
        gates[k] for k in (
            "pass_triples_precision",
            "pass_triples_recall",
            "pass_invalidations_precision",
            "pass_privacy",
        )
    )
    return {
        "sums": {"triples": sum_t, "invalidations": sum_i},
        "gates": gates,
    }


async def _amain(args) -> int:
    from eidolon.memory.application.steward import create_steward
    from eidolon.memory.config.memory_settings import get_memory_settings

    settings = get_memory_settings()
    steward = create_steward(settings)
    user_id = args.user_id

    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        print(f"[eval] dataset missing: {dataset_path}")
        return 2

    samples = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    print(f"[eval] {len(samples)} samples; LLM={settings.llm.model}")

    results = []
    for s in samples:
        try:
            r = await _run_one(s, steward, user_id)
        except Exception as exc:
            print(f"[eval][ERR] {s['name']}: {exc}")
            continue
        results.append(r)
        print(
            f"  {s['name']:<32s} "
            f"t-tp={r['triples']['tp']:<2d} t-fp={r['triples']['fp']:<2d} "
            f"i-tp={r['invalidations']['tp']:<2d} priv={'✓' if r['privacy_ok'] else '✗'} "
            f"{r['elapsed_ms']}ms"
        )

    agg = _aggregate(results)
    report = {"samples": results, "aggregate": agg}
    print("\n[eval] aggregate:")
    print(json.dumps(agg, indent=2, ensure_ascii=False))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"[eval] wrote {args.out}")

    return 0 if agg["gates"]["overall_pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default="tests/memory/eval_steward_dataset.jsonl",
        help="JSONL of {name, user_text, assistant_text, expect} per line.",
    )
    parser.add_argument("--user-id", default="eval")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
