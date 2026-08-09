"""One shared encoder against one private encoder per user, on this host.

The deployment question the ``embedding_server`` entrypoint answers, measured
rather than argued. The supervisor spawns one ``agent_runner`` per user, so every
user gets their own ONNX session — same weights, N copies, N×``threads`` threads
over however many cores the board has. Pointing them all at one server costs a
loopback hop per embed and buys back everything else.

Both arms are built the way the deployment builds them. The private arm really
does construct one ``OnnxSentenceEmbedder`` per simulated user, because a single
in-process session shared between threads is not what the supervisor gives you
and measuring that instead would answer a question nobody asked.

Start the server first, then run this against it::

    eidolon-memory-embedder --model bge-base-zh --model-dir ~/models/bge-base-zh &
    uv run python benchmarks/suites/probe_shared_embedder.py \\
        --model-dir ~/models/bge-base-zh --users 1,2,4,8,16

What it does **not** measure: retrieval quality. Both arms run the same weights
and return the same vectors to 3e-08, which this checks once at startup — if that
check ever fails, the two arms are no longer comparable and neither are the
palaces they would build.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "4")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hostinfo import describe  # noqa: E402

_QUERIES = [
    "我喜欢什么茶",
    "我养了什么宠物",
    "我在哪里工作",
    "我的家人是谁",
]


def _p50(values: list[float]) -> float:
    return round(sorted(values)[len(values) // 2], 1)


def _fan_out(call, users: int, rounds: int) -> float:
    """One round is every simulated user embedding one query at the same time.

    The wall clock of the round is the number that matters, not the per-call
    latency: a user waiting on a recall is waiting on their own call *and* on
    whatever the other users' calls did to the cores.
    """

    call(0, -1)  # warm, so a first-call session load is not read as contention
    walls = []
    with ThreadPoolExecutor(max_workers=users) as pool:
        for r in range(rounds):
            start = time.perf_counter()
            list(pool.map(lambda u: call(u, r), range(users)))
            walls.append((time.perf_counter() - start) * 1000)
    return _p50(walls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="bge-base-zh")
    parser.add_argument("--model-dir", default="", help="weights already on this host")
    parser.add_argument("--threads", type=int, default=4, help="per private session")
    parser.add_argument("--base-url", default="http://127.0.0.1:8760/v1")
    parser.add_argument("--users", default="1,2,4,8", help="comma-separated concurrency levels")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args(argv)

    from eidolon.memory.domain.embedding_port import local_model_spec
    from eidolon.memory.infrastructure.http_embedder import HttpEmbedder
    from eidolon.memory.infrastructure.onnx_sentence_embedder import OnnxSentenceEmbedder

    spec = local_model_spec(args.model.strip().lower())
    if spec is None:
        raise SystemExit(f"unknown local model {args.model!r}")
    model_dir = str(Path(args.model_dir).expanduser()) if args.model_dir else ""
    levels = [int(x) for x in args.users.split(",") if x.strip()]
    most = max(levels)

    def _private() -> OnnxSentenceEmbedder:
        return OnnxSentenceEmbedder(
            args.model, intra_op_num_threads=args.threads, model_dir=model_dir
        )

    def _shared() -> HttpEmbedder:
        return HttpEmbedder(
            base_url=args.base_url,
            model=args.model,
            dimension=spec.dimension,
            name=spec.collection_name or args.model,
        )

    host = describe()
    print(f"host   {host['cpu']} · {host['cpu_count']} cores · {host['total_ram_mb']} MB")
    print(f"model  {args.model}  ({spec.dimension}d, {args.threads} threads per private session)\n")

    # Same weights on both sides, or the comparison is meaningless and so is the
    # migration it recommends.
    probe_text = ["用户喜欢喝乌龙茶，不加糖"]
    try:
        remote = _shared().embed_documents(probe_text)
    except Exception as error:  # noqa: BLE001 - the message is the whole point
        raise SystemExit(
            f"no server at {args.base_url} ({type(error).__name__}: {error}). "
            f"Start it with: eidolon-memory-embedder --model {args.model}"
        ) from error
    direct = _private().embed_documents(probe_text)
    drift = max(abs(a - b) for a, b in zip(remote[0], direct[0], strict=True))
    if drift > 1e-5:
        raise SystemExit(
            f"the server returns different vectors than the local session "
            f"(max component delta {drift:.2e}). The server is not running "
            f"{args.model}, or not with these weights — the two arms are not "
            f"comparable and a palace built by one cannot be read by the other."
        )
    print(f"same weights both sides: max component delta {drift:.1e}\n")

    privates = [_private() for _ in range(most)]
    for e in privates:
        e.embed_documents(["预热"])
    shareds = [_shared() for _ in range(most)]

    print(f"{'callers':>8}  {'private/user':>14}  {'one server':>12}  {'ratio':>7}")
    for users in levels:
        a = _fan_out(
            lambda u, r: privates[u].embed_queries([f"{_QUERIES[u % 4]}#{r}"]),
            users,
            args.rounds,
        )
        b = _fan_out(
            lambda u, r: shareds[u].embed_queries([f"{_QUERIES[u % 4]}#{r}"]),
            users,
            args.rounds,
        )
        print(f"{users:>8}  {a:>11.1f} ms  {b:>9.1f} ms  {b / a:>6.2f}x")

    print(
        "\nratio < 1 means the shared server won. The loopback hop is the only "
        "cost and it shows up at one caller; every arm above that is contention."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
