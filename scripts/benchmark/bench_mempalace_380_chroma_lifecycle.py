"""Exercise MemPalace 3.8 + Chroma lifecycle through public APIs only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import resource
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

DIMENSION = 512
MODEL = "Xenova/bge-small-zh-v1.5"


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    vector = [0.0] * DIMENSION
    for index, value in enumerate(digest):
        vector[(index * 17 + value) % DIMENSION] += (value + 1) / 256.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _configure(palace: Path, home: Path) -> None:
    os.environ.update(
        {
            "HOME": str(home),
            "MEMPALACE_PALACE_PATH": str(palace),
            "MEMPALACE_BACKEND": "chroma",
            "MEMPALACE_EMBEDDING_MODEL": "openai-compat",
            "MEMPALACE_EMBEDDING_API_URL": "http://127.0.0.1:9",
            "MEMPALACE_EMBEDDING_API_MODEL": MODEL,
        }
    )


def _close(palace: Path) -> None:
    from mempalace.palace import get_backend_for_palace

    get_backend_for_palace(str(palace), explicit="chroma").close_palace(str(palace))


def _upsert(palace: Path, item_id: str, text: str) -> None:
    from mempalace.palace import get_collection

    collection = get_collection(str(palace), create=True, backend="chroma")
    collection.upsert(
        ids=[item_id],
        documents=[text],
        metadatas=[
            {
                "wing": "Wing_Profile",
                "room": "benchmark",
                "audience": "companion:benchmark",
            }
        ],
        embeddings=[_vector(f"passage: {text}")],
    )


def _query(palace: Path, text: str, *, item_id: str | None = None) -> list[str]:
    from mempalace.palace import get_collection

    collection = get_collection(
        str(palace), create=False, backend="chroma", read_only=True
    )
    if item_id is not None:
        return list(collection.get(ids=[item_id], include=["metadatas"]).ids)
    result = collection.query(
        query_embeddings=[_vector(f"query: {text}")],
        n_results=5,
        where={"audience": "companion:benchmark"},
        include=["documents", "metadatas", "distances"],
    )
    return list((result.ids or [[]])[0])


def _worker_write(palace: Path, home: Path, item_id: str) -> int:
    _configure(palace, home)
    _upsert(palace, item_id, f"external visibility marker {item_id}")
    _close(palace)
    return 0


def _worker_hold(palace: Path, home: Path) -> int:
    _configure(palace, home)
    _query(palace, "seed")
    print("READY", flush=True)
    while True:
        time.sleep(1)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "mean": statistics.fmean(values) if values else 0.0,
    }


def _child_command(mode: str, palace: Path, home: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        mode,
        "--palace",
        str(palace),
        "--home",
        str(home),
        *extra,
    ]


def _assert_child_recovery(palace: Path, home: Path, sig: signal.Signals) -> float:
    child = subprocess.Popen(
        _child_command("worker-hold", palace, home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    ready = child.stdout.readline().strip()
    if ready != "READY":
        raise RuntimeError(f"hold worker did not start: {ready!r}")
    os.kill(child.pid, sig)
    child.wait(timeout=10)
    started = time.perf_counter()
    _close(palace)
    if not _query(palace, "seed"):
        raise AssertionError(f"palace unreadable after {sig.name}")
    return (time.perf_counter() - started) * 1000


def _run(palace: Path, home: Path, *, seed: int, operations: int) -> dict[str, Any]:
    import chromadb
    import mempalace
    from mempalace.palace import get_collection

    if mempalace.__version__ != "3.8.0":
        raise RuntimeError(f"expected MemPalace 3.8.0, got {mempalace.__version__}")
    _configure(palace, home)

    writer = get_collection(str(palace), create=True, backend="chroma")
    batch = 100
    for offset in range(0, seed, batch):
        ids = [f"seed-{index}" for index in range(offset, min(offset + batch, seed))]
        docs = [f"seed memory {index}" for index in range(offset, min(offset + batch, seed))]
        writer.upsert(
            ids=ids,
            documents=docs,
            metadatas=[
                {
                    "wing": "Wing_Profile",
                    "room": "benchmark",
                    "audience": "companion:benchmark",
                }
                for _ in ids
            ],
            embeddings=[_vector(f"passage: {text}") for text in docs],
        )

    read_ms: list[float] = []
    write_ms: list[float] = []
    visibility_ms: list[float] = []
    writer_guard = threading.Lock()

    def read_one(index: int) -> None:
        started = time.perf_counter()
        if not _query(palace, f"seed memory {index % seed}"):
            raise AssertionError("concurrent query returned no rows")
        read_ms.append((time.perf_counter() - started) * 1000)

    def write_one(index: int) -> None:
        item_id = f"concurrent-{index}"
        with writer_guard:
            started = time.perf_counter()
            _upsert(palace, item_id, f"concurrent memory {index}")
            write_ms.append((time.perf_counter() - started) * 1000)
            visible_started = time.perf_counter()
            if _query(palace, item_id, item_id=item_id) != [item_id]:
                raise AssertionError("write was not visible to the shared reader")
            visibility_ms.append((time.perf_counter() - visible_started) * 1000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for index in range(operations):
            target = write_one if index % 5 == 0 else read_one
            futures.append(pool.submit(target, index))
        for future in as_completed(futures):
            future.result()

    # Hold a reader in this process, then mutate from another process. The next
    # public get_collection call must notice disk freshness and see the marker.
    _query(palace, "seed")
    external_id = "external-process-marker"
    subprocess.run(
        _child_command("worker-write", palace, home, "--item-id", external_id),
        check=True,
        timeout=30,
    )
    external_started = time.perf_counter()
    if _query(palace, external_id, item_id=external_id) != [external_id]:
        raise AssertionError("resident reader served a stale Chroma cache")
    external_visibility_ms = (time.perf_counter() - external_started) * 1000

    _close(palace)
    reopen_started = time.perf_counter()
    reopened_count = get_collection(
        str(palace), create=False, backend="chroma", read_only=True
    ).count()
    reopen_ms = (time.perf_counter() - reopen_started) * 1000
    _close(palace)

    snapshot = palace.with_name(f"{palace.name}-snapshot")
    shutil.copytree(palace, snapshot)
    snapshot_started = time.perf_counter()
    snapshot_count = get_collection(
        str(snapshot), create=False, backend="chroma", read_only=True
    ).count()
    snapshot_open_ms = (time.perf_counter() - snapshot_started) * 1000
    _close(snapshot)

    term_recovery_ms = _assert_child_recovery(palace, home, signal.SIGTERM)
    kill_recovery_ms = _assert_child_recovery(palace, home, signal.SIGKILL)
    _close(palace)

    db = palace / "chroma.sqlite3"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mib = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
    return {
        "mempalace": mempalace.__version__,
        "chromadb": chromadb.__version__,
        "backend": "chroma",
        "dimension": DIMENSION,
        "seed": seed,
        "operations": operations,
        "read_latency_ms": _summary(read_ms),
        "write_latency_ms": _summary(write_ms),
        "write_visibility_probe_ms": _summary(visibility_ms),
        "external_writer_visibility_ms": external_visibility_ms,
        "reopen_ms": reopen_ms,
        "snapshot_open_ms": snapshot_open_ms,
        "term_recovery_ms": term_recovery_ms,
        "kill_recovery_ms": kill_recovery_ms,
        "expected_count": seed + len(write_ms) + 1,
        "reopened_count": reopened_count,
        "snapshot_count": snapshot_count,
        "sqlite_integrity": integrity,
        "max_rss_mib": rss_mib,
        # 3.8 accepts the public read_only option. Its Chroma backend does not
        # advertise or implement a separate immutable/read-only client; readers
        # share the backend-managed PersistentClient and are isolated by Eidolon's
        # service-level reader/writer discipline.
        "chroma_native_read_only": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        nargs="?",
        default="run",
        choices=["run", "worker-write", "worker-hold"],
    )
    parser.add_argument("--palace")
    parser.add_argument("--home")
    parser.add_argument("--item-id")
    parser.add_argument("--seed", type=int, default=500)
    parser.add_argument("--operations", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.mode != "run":
        if not args.palace or not args.home:
            raise SystemExit("worker modes require --palace and --home")
        palace = Path(args.palace)
        home = Path(args.home)
        if args.mode == "worker-write":
            if not args.item_id:
                raise SystemExit("worker-write requires --item-id")
            raise SystemExit(_worker_write(palace, home, args.item_id))
        raise SystemExit(_worker_hold(palace, home))

    with tempfile.TemporaryDirectory(prefix="eidolon-mempalace-380-life-") as root:
        base = Path(root)
        palace = Path(args.palace).resolve() if args.palace else base / "palace"
        home = Path(args.home).resolve() if args.home else base / "home"
        home.mkdir(parents=True, exist_ok=True)
        print(
            json.dumps(
                _run(palace, home, seed=args.seed, operations=args.operations),
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
