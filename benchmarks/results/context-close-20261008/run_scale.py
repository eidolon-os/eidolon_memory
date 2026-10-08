"""Isolated scale experiment using production batch ingestion, Agent CLI and MCP probe."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from eidolon.memory.adapters.local_palace_router import LocalPalaceRouter  # noqa: E402
from eidolon.memory.application.discovery import probe_mcp_http  # noqa: E402
from eidolon.memory.config.memory_settings import MemorySettings  # noqa: E402
from eidolon.memory.domain.fragments import MemoryFragment  # noqa: E402
from scripts.benchmark.bench_read_livekit import _DEFAULT_QUERIES  # noqa: E402
from scripts.benchmark.manifest import build_manifest  # noqa: E402

OUT = Path(__file__).resolve().parent
RUN = Path("/private/tmp/eidolon-context-close-scale-20261008")
REALM = "context-close-scale"
PHRASES = [
    ("Wing_Life", "最近心情愉快，周末散步后放松了。"),
    ("Wing_Profile", "喜欢钢琴音乐，每晚听古典乐。"),
    ("Wing_Work", "最近工作压力很大，项目周五截止。"),
    ("Wing_Life", "和妈妈关系很好，每周一起吃饭。"),
    ("Wing_Life", "最近睡眠不足，计划早点休息。"),
    ("Wing_Profile", "兴趣爱好是徒步和摄影。"),
    ("Wing_Life", "上周参加了朋友的婚礼。"),
    ("Wing_Profile", "每天早上喝乌龙茶，不喝咖啡。"),
]


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def spawn(args, name, env):
    with (OUT / f"{name}.log").open("w") as log:
        return subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


async def seed(settings, lo, hi):
    router = LocalPalaceRouter(settings, allowed_spaces=[REALM])
    try:
        runtime = await router.resolve(REALM)
        for start in range(lo, hi, 128):
            fragments = []
            for i in range(start, min(start + 128, hi)):
                wing, phrase = PHRASES[i % 8]
                # Include unrelated-audience/device/private rows at every scale.
                fragments.append(
                    MemoryFragment(
                        memory_space_id=REALM,
                        owner_id="benchmark",
                        companion_id="benchmark",
                        audience="companion:other" if i % 37 == 0 else "companion:benchmark",
                        visibility="current_device" if i % 41 == 0 else "all_devices",
                        source_device_id="other-device" if i % 41 == 0 else "benchmark",
                        privacy="do_not_recall" if i % 43 == 0 else "normal",
                        source_turn_id=f"turn-{i}",
                        wing=wing,
                        room=f"fact-{i}",
                        content=f"用户{phrase} 记录编号{i}，这是第{i // 8}次记录。",
                        occurred_at="2026-09-01T08:00:00Z",
                        memory_type="fact",
                        importance=3,
                        confidence=1,
                    )
                )
            await runtime.backend.ingest_fragments(fragments)
        if lo == 0:
            for i, (_, phrase) in enumerate(PHRASES):
                await runtime.backend.ingest_text(
                    wing="Wing_Theme",
                    room=f"theme-{i}",
                    text=f"{_DEFAULT_QUERIES[i]}：用户{phrase}",
                    metadata={
                        "audience": "companion:benchmark",
                        "scope": "persona",
                        "visibility": "all_devices",
                        "memory_type": "theme",
                    },
                )
        for i in range(lo, hi):
            if i % 25 == 1:
                await runtime.kg.add_triple(
                    subject=_DEFAULT_QUERIES[i % 8],
                    predicate="has_context",
                    object=f"用户{PHRASES[i % 8][1]}，记录{i}",
                    audience="companion:benchmark",
                    source_turn_id=f"turn-{i}",
                )
        manifest = build_manifest(
            suite="isolated-mcp-scale",
            repo_root=ROOT,
            settings=settings,
            command=f"{sys.executable} {__file__}",
            palace_path=runtime.palace_path,
            scale={"facts": hi, "themes": 8, "graph": await runtime.kg.stats()},
            notes="Synthetic fixed Chinese categories, real HTTP bge-small-zh; no steward writes.",
        )
        manifest["source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (OUT / f"server-{hi}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    finally:
        await router.aclose()


async def ready(proc, url):
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with {proc.returncode}")
        if await probe_mcp_http(url, timeout_seconds=1):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError(f"MCP not ready: {url}")


def main():
    if RUN.exists():
        raise RuntimeError(f"Refusing to overwrite {RUN}")
    RUN.mkdir()
    (RUN / "empty.env").write_text("")
    settings = MemorySettings.model_validate(
        {
            "runtime": {"palaces_root": str(RUN / "palaces"), "run_dir": str(RUN / "run")},
            "embedding": {
                "provider": "http",
                "model": "bge-small-zh",
                "http": {
                    "base_url": "http://127.0.0.1:19783/v1",
                    "model": "bge-small-zh",
                    "dimension": 512,
                },
            },
            "steward": {"mode": "noop"},
            "nats": {"url": "nats://127.0.0.1:19722"},
            "mcp_http": {"host": "127.0.0.1", "port": 19730},
        }
    )
    (RUN / "settings.yaml").write_text(settings.model_dump_json(indent=2))
    (OUT / "settings.json").write_text(settings.model_dump_json(indent=2))
    env = dict(
        os.environ,
        EIDOLON_MEMORY_SETTINGS_YAML=str(RUN / "settings.yaml"),
        EIDOLON_MEMORY_ENV_FILE=str(RUN / "empty.env"),
        EIDOLON_MEMORY_RUN_DIR=str(RUN / "run"),
        EIDOLON_MEMORY_PALACES_ROOT=str(RUN / "palaces"),
        EIDOLON_MEMORY_PROCESS_TMP_ROOT=str(RUN / "process-tmp"),
        MEMPALACE_HOME=str(RUN / "home"),
        NO_PROXY="127.0.0.1,localhost",
        HF_HUB_OFFLINE="1",
    )
    os.environ.update(env)
    children = []
    outcomes = []
    try:
        nats = spawn(
            ["nats-server", "-a", "127.0.0.1", "-p", "19722", "-js", "-sd", str(RUN / "nats")],
            "nats",
            env,
        )
        children.append(nats)
        model_dir = (
            "/Users/manson/.cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5/"
            "snapshots/75c43b069aac4d136ba6bc1122f995fedcfd2781"
        )
        embedder = spawn(
            [
                str(Path(sys.executable).parent / "eidolon-memory-embedder"),
                "--model",
                "bge-small-zh",
                "--model-dir",
                model_dir,
                "--host",
                "127.0.0.1",
                "--port",
                "19783",
                "--threads",
                "4",
            ],
            "embedder",
            env,
        )
        children.append(embedder)
        for port, proc in [(19722, nats), (19783, embedder)]:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError(f"{port} process exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"{port} not ready")
        lo = 0
        for size in (5000,):
            asyncio.run(seed(settings, lo, size))
            lo = size
            print(f"Seeded {size} facts + 8 themes", flush=True)
            agent = spawn(
                [
                    str(Path(sys.executable).parent / "eidolon-memory-agent"),
                    "--memory-space-id",
                    REALM,
                    "--owner-id",
                    "benchmark",
                    "--port",
                    "19730",
                ],
                f"agent-{size}",
                env,
            )
            children.append(agent)
            asyncio.run(ready(agent, "http://127.0.0.1:19730/mcp"))
            for batch in (1, 2):
                for mode, flags in [
                    ("chat", []),
                    ("chat-graph", ["--with-kg"]),
                    ("voice", ["--voice", "--with-kg"]),
                ]:
                    for query_mode in ("repeated",):
                        name = f"{size}-{mode}-{query_mode}-b{batch}"
                        cmd = [
                            sys.executable,
                            "scripts/benchmark/bench_read_livekit.py",
                            "--url",
                            "http://127.0.0.1:19730/mcp",
                            "--memory-realm-id",
                            REALM,
                            "--owner-id",
                            "benchmark",
                            "--companion-id",
                            "benchmark",
                            "--count",
                            "24",
                            "--server-manifest",
                            str(OUT / f"server-{size}.json"),
                            "--out",
                            str(OUT / f"{name}.json"),
                            *flags,
                        ]
                        if query_mode == "novel":
                            cmd += ["--warmup", "0"]
                            for i in range(160):
                                cmd += [
                                    "--query",
                                    f"{_DEFAULT_QUERIES[i % 8]}，请回顾第{i}条"
                                    f"（{size}-{mode}-{batch}）",
                                ]
                        with (OUT / f"{name}.log").open("w") as log:
                            result = subprocess.run(
                                cmd, env=env, stdout=log, stderr=subprocess.STDOUT
                            )
                        outcomes.append({"run": name, "exit_code": result.returncode})
                        print(name, result.returncode, flush=True)
            stop(agent)
    finally:
        for proc in reversed(children):
            stop(proc)
        (OUT / "outcomes.json").write_text(json.dumps(outcomes, indent=2) + "\n")


if __name__ == "__main__":
    main()
