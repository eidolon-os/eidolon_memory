#!/usr/bin/env python3
"""Emit the U0 Palace inventory or a deep manifest for an offline copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eidolon.memory.infrastructure.palace_inventory import build_palaces_inventory


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palaces-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--offline-deep",
        action="store_true",
        help="Hash files and query SQLite counts; only use after stopping owners or on a copy",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    manifest = build_palaces_inventory(args.palaces_root, deep=args.offline_deep)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {manifest['palace_count']} Palace entries to {args.output} "
        f"(deep={manifest['deep_offline_manifest']})"
    )


if __name__ == "__main__":
    main()
