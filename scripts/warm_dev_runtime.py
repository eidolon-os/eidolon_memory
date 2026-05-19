#!/usr/bin/env python3
"""Prefetch local dev assets before run_all / Admin (embedding model, closets, search path)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _onnx_cache_dir() -> Path:
    return Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"


def warm_embedding() -> None:
    from mempalace.embedding import describe_device, get_embedding_function

    print("warming Chroma ONNX embedding (all-MiniLM-L6-v2, ~79MB on first run)…")
    ef = get_embedding_function()
    vectors = ef(["eidolon memory warmup"])
    dim = len(vectors[0]) if vectors else 0
    cache = _onnx_cache_dir()
    has_extracted = (cache / "onnx").is_dir()
    has_archive = (cache / "onnx.tar.gz").is_file()
    print(f"  device={describe_device()} embedding_dim={dim}")
    print(f"  cache={cache}")
    print(f"  extracted_onnx={has_extracted} archive={has_archive}")
    if not has_extracted and not has_archive:
        raise RuntimeError("embedding warmup finished but ONNX model cache is missing")


def warm_closets(palace_path: str) -> None:
    from mempalace.palace import get_closets_collection

    print("ensuring mempalace_closets (semantic search index layer)…")
    closets = get_closets_collection(palace_path, create=True)
    print(f"  closets drawer count={closets.count()}")


def warm_search(palace_path: str, *, all_wings: bool) -> None:
    from mempalace.searcher import search_memories

    from eidolon.memory.config.memory_settings import get_memory_settings

    settings = get_memory_settings()
    wings = [w.id for w in settings.wings if w.id != "Wing_Privacy"]
    if not all_wings:
        wings = wings[:1]

    print(f"dry-run semantic search ({len(wings)} wing(s))…")
    for wing_id in wings:
        data = search_memories(
            "warmup",
            palace_path=palace_path,
            wing=wing_id,
            n_results=1,
        )
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"search failed for {wing_id}: {data['error']}")
        n = len(data.get("results", [])) if isinstance(data, dict) else 0
        print(f"  {wing_id}: ok (hits={n})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", help="Absolute MemPalace dir (overrides --user-id).")
    parser.add_argument("--user-id", help="Resolve palace via memory settings.")
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Only download embedding + ensure closets; skip search dry-run",
    )
    parser.add_argument(
        "--all-wings",
        action="store_true",
        help="Dry-run search on every configured wing (default: first wing only)",
    )
    args = parser.parse_args()
    if not args.palace and not args.user_id:
        parser.error("either --palace <abs> or --user-id <id> is required")

    if args.palace:
        palace = args.palace
    else:
        from eidolon.memory.config.memory_settings import get_memory_settings
        from eidolon.memory.config.palace_directory import resolve_palace_for_user

        settings = get_memory_settings()
        palace = str(resolve_palace_for_user(settings, args.user_id))
    print("palace_path:", palace)

    warm_embedding()
    warm_closets(palace)
    if not args.skip_search:
        warm_search(palace, all_wings=args.all_wings)

    print("runtime warmup ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"runtime warmup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
