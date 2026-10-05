"""Reproduce the paired probe matrix; run from the memory repository."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
phase = sys.argv[1]
if phase not in ("before", "experiment", "after"):
    raise SystemExit("phase must be before, experiment or after")
os.chdir(ROOT)
os.environ.update(
    {
        "EIDOLON_MEMORY_SETTINGS_YAML": "/private/tmp/eidolon-recall-20261005b/settings.yaml",
        "EIDOLON_MEMORY_RUN_DIR": "/private/tmp/eidolon-recall-20261005b/run",
        "MEMPALACE_HOME": "/private/tmp/eidolon-recall-20261005b/home",
        "NO_PROXY": "127.0.0.1,localhost",
        "HF_HUB_OFFLINE": "1",
    }
)
source = (OUT / "variants" / ("serial.py" if phase == "before" else "parallel.py")
          if phase != "after" else ROOT / "eidolon/memory/application/public_recall.py")
files = [
    source,
    ROOT / "scripts/benchmark/probe_recall_stages.py",
    Path(os.environ["EIDOLON_MEMORY_SETTINGS_YAML"]),
]
(OUT / f"{phase}-sources.json").write_text(
    json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}, indent=2) + "\n"
)
for cache in ("warm", "uncached"):
    for mode, flags in [
        ("chat", ["--chat"]),
        ("chat-graph", ["--chat", "--with-kg"]),
        ("voice", ["--with-kg"]),
    ]:
        dest = OUT / f"{phase}-{mode}-{cache}.json"
        cmd = [
            sys.executable,
            "scripts/benchmark/probe_recall_stages.py",
            "--user-id",
            "recall-probe",
            "--count",
            "160",
            "--warmup",
            "8",
            "--out",
            str(dest),
            *flags,
        ]
        if phase != "after":
            cmd[1:2] = [str(OUT / "run_variant.py"), str(source)]
        if cache == "uncached":
            cmd.append("--clear-cache")
        with dest.with_suffix(".log").open("w") as log:
            completed = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
            if phase != "experiment":
                completed.check_returncode()
        data = json.loads(dest.read_text())
        print(dest.name, data["aggregate"]["stages_ms"]["recall_total_ms"], flush=True)
