"""Check a 3.9 Palace with 3.10, using only synthetic temporary data.

Run with the candidate interpreter and pass the preserved previous interpreter
as --previous-python. No existing Palace path is accepted by this probe.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_PHASE = r'''
import json, sqlite3, sys
from importlib.metadata import version
from mempalace.palace import get_backend_for_palace, get_collection

phase, palace = sys.argv[1:]
vector = [1.0] + [0.0] * 511
original = {"wing": "Wing_Profile", "room": "residence",
            "audience": "companion:xiaofang", "filed_at": "2026-09-30T09:54:50Z",
            "source_event_id": "synthetic-residence", "visibility": "all_devices"}
if phase == "seed":
    assert version("mempalace") == "3.9.0"
    col = get_collection(palace, create=True, backend="chroma")
    col.upsert(ids=["residence", "sibling"], documents=["用户住在北京", "mac的记忆"],
               metadatas=[original, {**original, "audience": "companion:mac"}],
               embeddings=[vector, vector])
else:
    assert version("mempalace") == ("3.10.0" if phase == "upgrade" else "3.9.0")
    col = get_collection(palace, create=False, backend="chroma", read_only=True)
    assert col.count() == 2
    row = col.get(ids=["residence"], include=["documents", "metadatas"])
    before_upgrade = phase in {"upgrade", "backup-read"}
    expected = "用户住在北京" if before_upgrade else "用户住在上海"
    assert row.documents == [expected], row
    assert all(row.metadatas[0][k] == v for k, v in original.items())
    hits = col.query(query_embeddings=[vector], n_results=2,
                     where={"audience": "companion:xiaofang"},
                     include=["documents", "metadatas", "distances"])
    expected_ids = {"residence"} if before_upgrade else {"residence", "new"}
    assert set(hits.ids[0]) == expected_ids, hits
    if phase == "upgrade":
        writer = get_collection(palace, create=False, backend="chroma")
        writer.upsert(ids=["residence"], documents=["用户住在上海"],
                      metadatas=[original], embeddings=[vector])
        writer.delete(ids=["sibling"])
        writer.upsert(ids=["new"], documents=["用户喜欢草莓"],
                      metadatas=[original], embeddings=[vector])
get_backend_for_palace(palace, explicit="chroma").close_palace(palace)
reopened = get_collection(palace, create=False, backend="chroma", read_only=True)
assert reopened.count() == 2
get_backend_for_palace(palace, explicit="chroma").close_palace(palace)
with sqlite3.connect("file:" + palace + "/chroma.sqlite3?mode=ro", uri=True) as db:
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print(json.dumps({"phase": phase, "mempalace": version("mempalace"), "count": 2,
                  "integrity": "ok"}))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-python", type=Path, required=True)
    args = parser.parse_args()
    previous = str(args.previous_python.absolute())
    with tempfile.TemporaryDirectory(prefix="eidolon-mempalace-upgrade-") as root:
        base = Path(root)
        home = base / "home"
        home.mkdir()
        palace = base / "palace"
        env = {
            **os.environ,
            "HOME": str(home),
            "MEMPALACE_CONFIG_DIR": str(home / ".mempalace"),
            "MEMPALACE_PALACE_PATH": str(palace),
            "MEMPALACE_BACKEND": "chroma",
            "MEMPALACE_EMBEDDING_MODEL": "openai-compat",
            "MEMPALACE_EMBEDDING_API_URL": "http://127.0.0.1:9",
            "MEMPALACE_EMBEDDING_API_MODEL": "Xenova/bge-small-zh-v1.5",
        }
        results = []
        for phase, interpreter in [("seed", previous), ("upgrade", sys.executable),
                                   ("rollback-read", previous), ("backup-read", previous)]:
            target = base / "before-upgrade" if phase == "backup-read" else palace
            completed = subprocess.run(
                [interpreter, "-c", _PHASE, phase, str(target)],
                env={**env, "MEMPALACE_PALACE_PATH": str(target)},
                capture_output=True, text=True, timeout=60,
            )
            if completed.returncode:
                raise RuntimeError(f"{phase} failed: {completed.stderr}\n{completed.stdout}")
            results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
            if phase == "seed":
                shutil.copytree(palace, base / "before-upgrade")
        print(json.dumps({"synthetic_only": True, "phases": results}, sort_keys=True))


if __name__ == "__main__":
    main()
