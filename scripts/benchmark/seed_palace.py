#!/usr/bin/env python3
"""Seed benchmark palace with dummy drawers (S/M/L sizes)."""

from __future__ import annotations

import argparse
import hashlib


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", required=True)
    parser.add_argument("--size", choices=["S", "M", "L"], default="M")
    args = parser.parse_args()

    counts = {"S": 100, "M": 1000, "L": 5000}
    n = counts[args.size]

    from mempalace.palace import get_collection

    col = get_collection(args.palace, create=True)
    batch = 50
    for start in range(0, n, batch):
        ids = []
        docs = []
        metas = []
        for i in range(start, min(start + batch, n)):
            wing = "Wing_Profile"
            room = "profile_core"
            text = f"benchmark drawer {i} preference tea morning"
            digest = hashlib.sha256(text.encode()).hexdigest()[:24]
            ids.append(f"drawer_{wing}_{room}_{digest}")
            docs.append(text)
            metas.append(
                {
                    "wing": wing,
                    "room": room,
                    "user_id": "bench",
                    "added_by": "seed_palace",
                }
            )
        col.upsert(ids=ids, documents=docs, metadatas=metas)
    print(f"seeded {n} drawers into {args.palace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
