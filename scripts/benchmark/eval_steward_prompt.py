#!/usr/bin/env python3
"""Score steward LLM prompt against a hand-labelled dataset (T2 §4.7).

Workflow:
1. Load JSONL dataset where each line has:
     {"name": str,
      "user_text": str, "assistant_text": str,
      "expect": {
          "triples":           [{"subject":..,"predicate":..,"object":..}, ...],
          "invalidations":     [{"subject":..,"predicate":..,"object":..}, ...],
          "privacy_action":    str | null,  # do_not_store/archive_topic/delete_request/null
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


def _mention_key(m: dict | object) -> tuple[str, str]:
    """Phase 3 — score mentions on (entity_id, alias) pairs."""
    if isinstance(m, dict):
        return (m["entity_id"], m["alias"])
    return (m.entity_id, m.alias)


def _confusion(expected: list, actual: list) -> tuple[int, int, int]:
    """Returns (tp, fp, fn) where keys are triple tuples."""
    e = {_triple_key(x) for x in expected}
    a = {_triple_key(x) for x in actual}
    tp = len(e & a)
    fp = len(a - e)
    fn = len(e - a)
    return tp, fp, fn


def _mention_confusion(expected: list, actual: list) -> tuple[int, int, int]:
    """Same shape as ``_confusion`` but for (entity_id, alias) pairs."""
    e = {_mention_key(x) for x in expected}
    a = {_mention_key(x) for x in actual}
    return len(e & a), len(a - e), len(e - a)


def _triple_diff(expected: list, actual: list) -> dict[str, list[dict[str, str]]]:
    expected_keys = {_triple_key(item) for item in expected}
    actual_keys = {_triple_key(item) for item in actual}

    def rows(keys: set[tuple[str, str, str]]) -> list[dict[str, str]]:
        return [
            {"subject": subject, "predicate": predicate, "object": object_}
            for subject, predicate, object_ in sorted(keys)
        ]

    return {
        "expected": rows(expected_keys),
        "actual": rows(actual_keys),
        "false_positive": rows(actual_keys - expected_keys),
        "false_negative": rows(expected_keys - actual_keys),
    }


def _mention_diff(expected: list, actual: list) -> dict[str, list[dict[str, str]]]:
    expected_keys = {_mention_key(item) for item in expected}
    actual_keys = {_mention_key(item) for item in actual}

    def rows(keys: set[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"entity_id": entity_id, "alias": alias} for entity_id, alias in sorted(keys)]

    return {
        "expected": rows(expected_keys),
        "actual": rows(actual_keys),
        "false_positive": rows(actual_keys - expected_keys),
        "false_negative": rows(expected_keys - actual_keys),
    }


def _precision_recall(tp: int, fp: int, fn: int) -> tuple[float, float]:
    p = tp / (tp + fp) if (tp + fp) > 0 else 1.0  # no FP if nothing produced
    r = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    return p, r


def _error_rates(*, tp: int, fp: int, fn: int) -> tuple[float, float]:
    """Return hallucination and omission rates for one memory operation."""

    hallucination = fp / (tp + fp) if (tp + fp) > 0 else 0.0
    omission = fn / (tp + fn) if (tp + fn) > 0 else 0.0
    return hallucination, omission


def _validate_samples(samples: list[dict]) -> None:
    """Keep live labels explicit, unique, and within the wire predicate set."""

    from eidolon_memory_contracts import KG_PREDICATE_VALUES

    predicates = set(KG_PREDICATE_VALUES)
    seen: set[str] = set()
    placeholders = {"unknown", "none", "null", "未知"}
    for index, sample in enumerate(samples, 1):
        name = str(sample.get("name") or f"line {index}")
        if not sample.get("name") or not sample.get("category"):
            raise ValueError(f"{name}: name and category are required")
        if name in seen:
            raise ValueError(f"{name}: duplicate sample name")
        seen.add(name)
        expect = sample.get("expect")
        if not isinstance(expect, dict) or not isinstance(expect.get("should_write"), bool):
            raise ValueError(f"{name}: expect.should_write must be boolean")
        triples = list(expect.get("triples") or [])
        invalidations = list(expect.get("invalidations") or [])
        if not expect["should_write"] and (triples or invalidations):
            raise ValueError(f"{name}: no-write sample cannot label writes")
        for item in triples + invalidations:
            predicate = str(item.get("predicate") or "")
            if predicate not in predicates:
                raise ValueError(f"{name}: unsupported predicate {predicate!r}")
            if str(item.get("object") or "").strip().lower() in placeholders:
                raise ValueError(f"{name}: placeholder objects are not evidence")


async def _run_one(sample: dict, steward, user_id: str) -> dict:
    from eidolon_memory_contracts import ConversationTurnPayload, build_memory_actor_context

    turn = ConversationTurnPayload(
        turn_id=uuid.uuid4().hex,
        context=build_memory_actor_context(
            owner_id=user_id,
            companion_id="steward-eval",
            memory_realm_id=f"r:{user_id}:steward-eval",
            device_id="steward-eval",
            session_id="eval",
        ),
        timestamp="2026-05-19T10:00:00Z",
        user_text=sample["user_text"],
        assistant_text=sample.get("assistant_text", ""),
    )
    t0 = time.perf_counter()
    decision = await steward.decide(turn)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    expect = sample["expect"]
    tp_t, fp_t, fn_t = _confusion(expect.get("triples", []), decision.triples)
    tp_i, fp_i, fn_i = _confusion(expect.get("invalidations", []), decision.invalidations)
    # Phase 3 — mentions scored independently. Samples without an "mentions"
    # key are treated as expecting none (so a steward that outputs nothing
    # there is correct, not penalised).
    tp_m, fp_m, fn_m = _mention_confusion(
        expect.get("mentions", []),
        getattr(decision, "mentions", None) or [],
    )

    privacy_expected = expect.get("privacy_action") or None
    privacy_actual = decision.privacy_actions[0].action if decision.privacy_actions else None
    privacy_ok = privacy_expected == privacy_actual

    return {
        "name": sample["name"],
        "category": sample.get("category") or "uncategorized",
        "elapsed_ms": round(elapsed_ms, 1),
        "should_write_expected": expect.get("should_write"),
        "should_write_actual": decision.should_write,
        "should_write_ok": expect.get("should_write") is decision.should_write,
        "triples": {"tp": tp_t, "fp": fp_t, "fn": fn_t},
        "triple_diff": _triple_diff(expect.get("triples", []), decision.triples),
        "invalidations": {"tp": tp_i, "fp": fp_i, "fn": fn_i},
        "invalidation_diff": _triple_diff(expect.get("invalidations", []), decision.invalidations),
        "mentions": {"tp": tp_m, "fp": fp_m, "fn": fn_m},
        "mention_diff": _mention_diff(
            expect.get("mentions", []), getattr(decision, "mentions", None) or []
        ),
        "privacy_ok": privacy_ok,
        "privacy_expected": privacy_expected,
        "privacy_actual": privacy_actual,
    }


def _aggregate(results: list[dict], *, requested_count: int | None = None) -> dict:
    sum_t = {"tp": 0, "fp": 0, "fn": 0}
    sum_i = {"tp": 0, "fp": 0, "fn": 0}
    sum_m = {"tp": 0, "fp": 0, "fn": 0}
    privacy_misses = 0
    privacy_errors = 0
    privacy_expected_count = 0
    should_write_correct = 0
    should_write_total = 0
    for r in results:
        for k in ("tp", "fp", "fn"):
            sum_t[k] += r["triples"][k]
            sum_i[k] += r["invalidations"][k]
            sum_m[k] += r.get("mentions", {}).get(k, 0)
        if r.get("should_write_expected") is not None:
            should_write_total += 1
            should_write_correct += int(bool(r.get("should_write_ok")))
        if not r["privacy_ok"]:
            privacy_errors += 1
        if r["privacy_expected"]:
            privacy_expected_count += 1
            if not r["privacy_ok"]:
                privacy_misses += 1

    tp, rp = _precision_recall(**sum_t)
    ip, ir = _precision_recall(**sum_i)
    mp, mr = _precision_recall(**sum_m)
    triple_hallucination, triple_omission = _error_rates(**sum_t)
    update_hallucination, update_omission = _error_rates(**sum_i)
    should_write_accuracy = should_write_correct / max(1, should_write_total)

    requested = len(results) if requested_count is None else requested_count
    gates = {
        "requested_samples": requested,
        "evaluated_samples": len(results),
        "failed_samples": max(0, requested - len(results)),
        "triples_precision": round(tp, 3),
        "triples_recall": round(rp, 3),
        "invalidations_precision": round(ip, 3),
        "invalidations_recall": round(ir, 3),
        "mentions_precision": round(mp, 3),
        "mentions_recall": round(mr, 3),
        "triple_hallucination_rate": round(triple_hallucination, 3),
        "triple_omission_rate": round(triple_omission, 3),
        "update_hallucination_rate": round(update_hallucination, 3),
        "update_omission_rate": round(update_omission, 3),
        "should_write_accuracy": round(should_write_accuracy, 3),
        "should_write_correct": should_write_correct,
        "should_write_total": should_write_total,
        "privacy_misses": privacy_misses,
        "privacy_errors": privacy_errors,
        "privacy_expected_count": privacy_expected_count,
        "pass_triples_precision": tp >= 0.85,
        "pass_triples_recall": rp >= 0.70,
        "pass_invalidations_precision": ip >= 0.90,
        # Phase 3 plan: mentions precision ≥ 0.80 (don't let LLM hallucinate
        # alias→entity bindings) — recall is informational only.
        "pass_mentions_precision": mp >= 0.80,
        "pass_should_write": should_write_accuracy >= 0.90,
        "pass_privacy": privacy_errors == 0,
        "pass_coverage": requested > 0 and len(results) == requested,
    }
    gates["overall_pass"] = all(
        gates[k]
        for k in (
            "pass_triples_precision",
            "pass_triples_recall",
            "pass_invalidations_precision",
            "pass_mentions_precision",
            "pass_should_write",
            "pass_privacy",
            "pass_coverage",
        )
    )
    return {
        "sums": {"triples": sum_t, "invalidations": sum_i, "mentions": sum_m},
        "categories": _category_breakdown(results),
        "gates": gates,
    }


def _category_breakdown(results: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for result in results:
        grouped.setdefault(result.get("category") or "uncategorized", []).append(result)

    report: dict[str, dict] = {}
    for category, items in sorted(grouped.items()):
        triples = {key: sum(item["triples"][key] for item in items) for key in ("tp", "fp", "fn")}
        invalidations = {
            key: sum(item["invalidations"][key] for item in items) for key in ("tp", "fp", "fn")
        }
        t_hallucination, t_omission = _error_rates(**triples)
        i_hallucination, i_omission = _error_rates(**invalidations)
        report[category] = {
            "samples": len(items),
            "should_write_accuracy": round(
                sum(bool(item.get("should_write_ok")) for item in items) / max(1, len(items)),
                3,
            ),
            "triple_hallucination_rate": round(t_hallucination, 3),
            "triple_omission_rate": round(t_omission, 3),
            "update_hallucination_rate": round(i_hallucination, 3),
            "update_omission_rate": round(i_omission, 3),
        }
    return report


async def _amain(args) -> int:
    from eidolon.memory.application.steward import create_steward
    from eidolon.memory.config.memory_settings import get_memory_settings

    settings = get_memory_settings()
    # The evaluator uses the same semantic-only extraction policy as production.
    # Network/model failures are errors, never substitute decisions.
    eval_settings = settings.model_copy(
        update={"steward": settings.steward.model_copy(update={"mode": "llm"})}
    )
    steward = create_steward(eval_settings)
    user_id = args.user_id

    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        print(f"[eval] dataset missing: {dataset_path}")
        return 2

    samples = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    try:
        _validate_samples(samples)
    except ValueError as exc:
        print(f"[eval] invalid dataset: {exc}")
        return 2
    print(f"[eval] {len(samples)} samples; LLM={eval_settings.llm.model}")

    results = []
    errors: list[dict[str, str]] = []
    for s in samples:
        try:
            r = await _run_one(s, steward, user_id)
        except Exception as exc:
            print(f"[eval][ERR] {s['name']}: {exc}")
            errors.append({"name": s["name"], "error": str(exc)})
            continue
        results.append(r)
        print(
            f"  {s['name']:<32s} "
            f"t-tp={r['triples']['tp']:<2d} t-fp={r['triples']['fp']:<2d} "
            f"i-tp={r['invalidations']['tp']:<2d} "
            f"m-tp={r['mentions']['tp']:<2d} m-fp={r['mentions']['fp']:<2d} "
            f"priv={'✓' if r['privacy_ok'] else '✗'} "
            f"{r['elapsed_ms']}ms"
        )

    agg = _aggregate(results, requested_count=len(samples))
    report = {
        "evaluation": {
            "requested": len(samples),
            "succeeded": len(results),
            "failed": len(errors),
        },
        "samples": results,
        "errors": errors,
        "aggregate": agg,
    }
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
