#!/usr/bin/env python3
"""Report how the household Realm's earlier records would be classified (design §3.6).

Read-only. Point it at a *copy* of the household Realm's palace, never the live
one: the live palace belongs to its running worker. Every record written
before the layer existed is read again by the production steward and given the
layer it would get as a new turn (``household_classification``). Nothing is
written to the palace; the report says what an execution would change. It
carries the records' text, so keep it on this machine.

    EIDOLON_MEMORY_RUN_LIVE=1 .venv/bin/python -I scripts/household_reclassification_report.py \\
        --palace <copy> --memory-space-id <realm> --owner-id <owner> --out <report.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend  # noqa: E402
from eidolon.memory.application.household_classification import classify_unlayered  # noqa: E402
from eidolon.memory.application.mempalace_hierarchy import scan_records  # noqa: E402
from eidolon.memory.application.steward import create_steward  # noqa: E402
from eidolon.memory.config.memory_settings import get_memory_settings  # noqa: E402
from eidolon.memory.infrastructure.mempalace_backend import (  # noqa: E402
    apply_mempalace_backend_env,
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", type=Path, required=True)
    parser.add_argument("--memory-space-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("EIDOLON_MEMORY_RUN_LIVE") != "1":
        print("set EIDOLON_MEMORY_RUN_LIVE=1: this calls the configured model", file=sys.stderr)
        return 2
    settings = get_memory_settings()
    # As the worker does: the palace records which embedder built it.
    apply_mempalace_backend_env(settings)
    backend = MemPalacePythonBackend(
        settings, str(args.palace), memory_space_id=args.memory_space_id
    )
    records, truncated = await scan_records(backend, max_records=10_000)
    classified = await classify_unlayered(
        records,
        steward=create_steward(settings),
        memory_space_id=args.memory_space_id,
        owner_id=args.owner_id,
    )
    report = {
        "memory_space_id": args.memory_space_id,
        "records": len(records),
        "truncated": truncated,
        "already_classified": sum(bool((r.metadata or {}).get("layer")) for r in records),
        "classified": dict(Counter(c.layer for c in classified)),
        "would_become_recallable": [
            {"entry_id": c.entry_id, "preview": c.preview}
            for c in classified
            if c.layer == "household"
        ],
        "would_stay_unclaimed": [
            {"entry_id": c.entry_id, "preview": c.preview}
            for c in classified
            if c.layer == "personal"
        ],
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {k: report[k] for k in ("records", "truncated", "already_classified", "classified")}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
