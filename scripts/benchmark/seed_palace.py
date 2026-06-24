#!/usr/bin/env python3
"""Seed benchmark palace with dummy drawers (S/M/L sizes)."""

from __future__ import annotations

import argparse
import hashlib
import math
import re

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _deterministic_embedding(text: str, *, dim: int = 64) -> list[float]:
    vector = [0.0] * dim
    tokens = _TOKEN_RE.findall((text or "").lower()) or [text or ""]
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        idx = digest[0] % dim
        sign = 1.0 if digest[1] % 2 == 0 else -1.0
        vector[idx] += sign * (1.0 + digest[2] / 255.0)
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", required=True)
    parser.add_argument("--size", choices=["S", "M", "L"], default="M")
    parser.add_argument("--memory-space-id", default="default.bench.mochi")
    parser.add_argument("--device-id", default="bench-device")
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
        embeddings = []
        for i in range(start, min(start + batch, n)):
            wing = "Wing_Profile"
            room = "profile_core"
            text = f"benchmark drawer {i} preference tea morning"
            digest = hashlib.sha256(text.encode()).hexdigest()[:24]
            ids.append(f"drawer_{wing}_{room}_{digest}")
            docs.append(text)
            embeddings.append(_deterministic_embedding(text))
            metas.append(
                {
                    "wing": wing,
                    "room": room,
                    "memory_space_id": args.memory_space_id,
                    "scope": "persona",
                    "visibility": "all_devices",
                    "source_device_id": args.device_id,
                    "source_instance_id": "seed_palace",
                    "session_id": "seed",
                    "memory_type": "preference",
                    "added_by": "seed_palace",
                }
            )
        col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)
    print(f"seeded {n} drawers into {args.palace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
