#!/usr/bin/env python3
"""Measure the steward's personal / household classification (design §3.6, A2).

Each row of the fixed set marks the spans of one utterance that are personal
and the spans that are household. Every claim the steward returns quotes the
user verbatim (``evidence_quote``), so a claim belongs to the span its quote
lies on. The household Realm keeps only claims labelled household, so:

- a personal span covered by a household claim is a leak (≤ 2% required): one
  person's fact would be told to the whole household;
- a household span covered by a household claim is kept (≥ 80% required).

"Don't tell anyone" and unclear rows are personal throughout. The turn is
built as the Agent publishes it to the household Realm: no person, the
Companion and device of a household speaker. Production prompt and model; no
Realm, store or bus is touched.

    EIDOLON_MEMORY_RUN_LIVE=1 .venv/bin/python scripts/benchmark/eval_household_layer.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eidolon_memory_contracts import ConversationTurnPayload, MemoryActorContext  # noqa: E402

from eidolon.memory.application.steward import create_steward  # noqa: E402
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402

DATASET = _ROOT / "tests/memory/eval_household_layer.jsonl"
REPORT = _ROOT / "reports/household_layer_eval.json"
MAX_LEAK = 0.02
MIN_KEPT = 0.80


def _turn(row: dict) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        turn_id=f"eval-{row['id']}",
        context=MemoryActorContext(
            memory_realm_id="r_eval_household",
            owner_id="owner-eval",
            companion_id="companion-eval",
            device_id="box-3",
            session_id="eval-session",
        ),
        timestamp="2026-11-04T10:00:00Z",
        user_text=row["text"],
        assistant_text="",
        metadata={
            "memory_ingest_policy": "semantic_steward",
            "source_component": "turn_observer",
            "privacy_mode": "normal",
        },
    )


def _covers(text: str, span: str, quote: str) -> bool:
    """Whether a claim quoting ``quote`` is about ``span`` of ``text``."""

    start, end = text.find(span), text.find(span) + len(span)
    q_start = text.find(quote)
    if q_start < 0 or start < 0:
        return False
    q_end = q_start + len(quote)
    overlap = max(0, min(end, q_end) - max(start, q_start))
    return overlap >= 0.5 * len(span) or overlap >= 0.5 * len(quote)


async def _run(row: dict, steward, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        started = time.perf_counter()
        try:
            decision = await steward.decide(_turn(row))
        except Exception as exc:  # noqa: BLE001 - a failed call is a result
            return {"id": row["id"], "kind": row["kind"], "error": f"{type(exc).__name__}: {exc}"}
    claims = [(f.layer, f.evidence_quote, f.content) for f in decision.fragments]
    # Graph facts are personal by construction: a household claim never becomes one.
    claims += [("personal", t.evidence_quote, t.statement or "") for t in decision.triples]
    household = [quote for layer, quote, _ in claims if layer == "household"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "text": row["text"],
        "elapsed_s": round(time.perf_counter() - started, 1),
        "claims": [{"layer": layer, "quote": q, "content": c} for layer, q, c in claims],
        "leaked": [
            s for s in row["personal"] if any(_covers(row["text"], s, q) for q in household)
        ],
        "kept": [s for s in row["household"] if any(_covers(row["text"], s, q) for q in household)],
        "household_as_personal": [
            s
            for s in row["household"]
            if not any(_covers(row["text"], s, q) for q in household)
            and any(_covers(row["text"], s, q) for layer, q, _ in claims if layer == "personal")
        ],
        "personal_spans": len(row["personal"]),
        "household_spans": len(row["household"]),
    }


def _summary(results: list[dict]) -> dict:
    ok = [r for r in results if "error" not in r]
    personal = sum(r["personal_spans"] for r in ok)
    household = sum(r["household_spans"] for r in ok)
    leaked = sum(len(r["leaked"]) for r in ok)
    kept = sum(len(r["kept"]) for r in ok)
    by_kind = {}
    for kind in sorted({r["kind"] for r in ok}):
        rows = [r for r in ok if r["kind"] == kind]
        by_kind[kind] = {
            "rows": len(rows),
            "leaked": sum(len(r["leaked"]) for r in rows),
            "kept": sum(len(r["kept"]) for r in rows),
            "household_spans": sum(r["household_spans"] for r in rows),
            "household_as_personal": sum(len(r["household_as_personal"]) for r in rows),
            "no_claim": sum(not r["claims"] for r in rows),
        }
    leak_rate = leaked / personal if personal else 0.0
    kept_rate = kept / household if household else 0.0
    return {
        "rows": len(results),
        "errors": Counter(r["error"].split(":")[0] for r in results if "error" in r),
        "personal_spans": personal,
        "leaked": leaked,
        "leak_rate": round(leak_rate, 4),
        "household_spans": household,
        "kept": kept,
        "kept_rate": round(kept_rate, 4),
        # A row the steward could not answer is a failed extraction, reported
        # above; it is not a classification, so it neither leaks nor keeps.
        "passes": leak_rate <= MAX_LEAK and kept_rate >= MIN_KEPT,
        "by_kind": by_kind,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--out", type=Path, default=REPORT)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    if os.environ.get("EIDOLON_MEMORY_RUN_LIVE") != "1":
        print("set EIDOLON_MEMORY_RUN_LIVE=1: this calls the configured model", file=sys.stderr)
        return 2
    settings = get_memory_settings()
    steward = create_steward(settings)
    rows = [
        json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line
    ]
    semaphore = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(_run(row, steward, semaphore) for row in rows))
    report = {
        "model": settings.llm.model,
        "extraction_version": getattr(steward, "extraction_version", ""),
        "summary": _summary(results),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
    return 0 if report["summary"]["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
