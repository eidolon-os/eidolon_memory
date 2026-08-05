#!/usr/bin/env python3
"""Prefetch local dev assets before Admin startup (embedding model, closets, search path)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _minilm_cache_dir() -> Path:
    """Where Chroma extracts MiniLM, which is the only model that lands there.

    Everything else — ours and embeddinggemma — is fetched by huggingface_hub into
    its own cache. So this path only answers a question about minilm, and checking
    it for any other model reports a missing cache after a successful warmup.
    """

    return Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"


def warm_embedding() -> None:
    from mempalace.embedding import describe_device

    from eidolon.memory.config.memory_settings import get_memory_settings
    from eidolon.memory.infrastructure.embedder_factory import active_embedder
    from eidolon.memory.infrastructure.mempalace_backend import apply_mempalace_backend_env

    settings = get_memory_settings()
    apply_mempalace_backend_env(settings)
    model = settings.embedding.model or "minilm"
    print(f"warming embedding ({model}, provider={settings.embedding.resolved_provider()})…")
    # Through the port, so this warms whatever is configured rather than whatever
    # MemPalace resolves — those are the same thing only when registration worked,
    # and this script is one of the places you would run to find out that it did.
    vectors = active_embedder().embed_documents(["eidolon memory warmup"])
    dim = len(vectors[0]) if vectors else 0
    print(f"  device={describe_device()} embedding_dim={dim}")

    expected = settings.embedding.declared_identity()
    if dim == 0:
        raise RuntimeError(f"embedding warmup returned no vector for model {model!r}")
    if expected is not None and dim != expected.dimension:
        # The width a warmup actually produced against the width a palace would
        # be created at. This is the assertion the old minilm-cache check was
        # standing in for, and it holds for every provider — including a hosted
        # one, which has no local cache to inspect at all.
        raise RuntimeError(
            f"embedding warmup produced {dim}-dimensional vectors but the "
            f"configuration declares {expected.dimension} for {model!r}. A palace "
            f"created now would be built at the declared width and reject writes."
        )

    if model == "minilm":
        cache = _minilm_cache_dir()
        has_extracted = (cache / "onnx").is_dir()
        has_archive = (cache / "onnx.tar.gz").is_file()
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
