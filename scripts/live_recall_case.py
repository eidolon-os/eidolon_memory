#!/usr/bin/env python3
"""Recall memories directly through the Eidolon MemPalace Python adapter."""

from __future__ import annotations

import argparse
import asyncio

from eidolon.memory.adapters.mempalace_python_backend import MemPalacePythonBackend
from eidolon.memory.config.memory_settings import get_memory_settings
from eidolon.memory.config.palace_directory import resolve_palace_directory


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Natural language recall query.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--wing", default="", help="Optional single wing to search.")
    parser.add_argument("--room", default="", help="Optional MemPalace room.")
    args = parser.parse_args()

    settings = get_memory_settings()
    backend = MemPalacePythonBackend(settings, str(resolve_palace_directory(settings)))
    wings = [args.wing] if args.wing else [w.id for w in settings.wings if w.id != "Wing_Privacy"]
    all_hits = []
    for wing in wings:
        hits = await backend.search(
            args.query,
            wing=wing,
            n_results=args.top_k,
            room=args.room or None,
        )
        all_hits.extend(hits)
    for index, hit in enumerate(all_hits[: args.top_k], start=1):
        print(f"\n[{index}] wing={hit.metadata.get('wing', hit.user_id)} room={hit.key}")
        print(f"metadata={hit.metadata}")
        print(f"value={hit.value}")
    if not all_hits:
        print("no hits")


if __name__ == "__main__":
    asyncio.run(main())
