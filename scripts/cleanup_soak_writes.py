#!/usr/bin/env python3
"""Remove benchmark / mixed-soak drawers from a MemPalace palace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SOAK_MARKERS = (
    "mixed soak write",
    "benchmark write sample",
    "bench message",
)

# Optional: pass --include-seed-bench to also remove seed_palace.py rows ("benchmark drawer …").
SEED_BENCH_MARKER = "benchmark drawer"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", help="Absolute palace directory (overrides --user-id).")
    parser.add_argument("--user-id", help="Resolve palace via memory settings.")
    parser.add_argument("--dry-run", action="store_true", help="List ids only, do not delete")
    parser.add_argument(
        "--include-seed-bench",
        action="store_true",
        help=f"Also delete documents containing {SEED_BENCH_MARKER!r}",
    )
    args = parser.parse_args()
    if not args.palace and not args.user_id:
        parser.error("either --palace <abs> or --user-id <id> is required")

    markers = SOAK_MARKERS
    if args.include_seed_bench:
        markers = (*SOAK_MARKERS, SEED_BENCH_MARKER)

    if args.palace:
        palace = args.palace
    else:
        from eidolon.memory.config.memory_settings import get_memory_settings
        from eidolon.memory.config.palace_directory import resolve_palace_for_user

        settings = get_memory_settings()
        palace = str(resolve_palace_for_user(settings, args.user_id))
    from mempalace.palace import get_collection

    col = get_collection(palace, create=False)
    result = col.get(include=["documents", "metadatas"])
    ids = result.get("ids") if isinstance(result, dict) else getattr(result, "ids", [])
    docs = result.get("documents") if isinstance(result, dict) else getattr(result, "documents", [])
    if ids and isinstance(ids[0], list):
        ids = ids[0]
        docs = docs[0] if docs else []

    to_delete: list[str] = []
    for drawer_id, doc in zip(ids, docs, strict=False):
        text = (doc or "").lower()
        if any(marker in text for marker in markers):
            to_delete.append(drawer_id)

    print(f"palace={palace}")
    print(f"matched={len(to_delete)}")
    for did in to_delete[:20]:
        print(f"  {did}")
    if len(to_delete) > 20:
        print(f"  ... and {len(to_delete) - 20} more")

    if args.dry_run or not to_delete:
        return 0

    col.delete(ids=to_delete)
    print(f"deleted={len(to_delete)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
