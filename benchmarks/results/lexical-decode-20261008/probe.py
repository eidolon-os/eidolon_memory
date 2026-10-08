"""Exact-output A/B of upstream row decoding; synthetic SQLite only, no embeddings."""

import hashlib
import inspect
import json
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path("/private/tmp/eidolon-mempalace-3.10-20261008")
sys.path.insert(0, str(SOURCE))
from mempalace.backends import chroma  # noqa: E402

OUT = Path(__file__).resolve().parent
new = chroma.ChromaCollection._lexical_search_via_sqlite
text = subprocess.check_output(
    ["git", "show", "HEAD:mempalace/backends/chroma.py"], cwd=SOURCE
).decode()
start = text.index("    def _lexical_search_via_sqlite(")
end = text.index("\n    @property", start)
namespace = dict(vars(chroma))
exec(compile(textwrap.dedent(text[start:end]), "<upstream-3.10.0>", "exec"), namespace)
old = namespace["_lexical_search_via_sqlite"]
report = {
    "upstream_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE)
    .decode()
    .strip(),
    "old_method_sha256": hashlib.sha256(text[start:end].encode()).hexdigest(),
    "new_method_sha256": hashlib.sha256(inspect.getsource(new).encode()).hexdigest(),
    "cases": [],
}
with tempfile.TemporaryDirectory(prefix="eidolon-lexical-decode-") as directory:
    conn = sqlite3.connect(Path(directory) / "chroma.sqlite3")
    conn.executescript("""
      CREATE TABLE collections(id INTEGER PRIMARY KEY, name TEXT);
      CREATE TABLE segments(id INTEGER PRIMARY KEY, collection INTEGER);
      CREATE TABLE embeddings(id INTEGER PRIMARY KEY, segment_id INTEGER,
          embedding_id TEXT, created_at TEXT);
      CREATE TABLE embedding_metadata(id INTEGER, key TEXT, string_value TEXT,
          int_value INTEGER, float_value REAL, bool_value INTEGER, PRIMARY KEY(id,key));
      CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(string_value);
      INSERT INTO collections VALUES(1,'drawers');
      INSERT INTO segments VALUES(1,1);
    """)
    for i in range(10000):
        doc = f"needle common token {i} " + ("needle " * (i % 4))
        conn.execute("INSERT INTO embeddings VALUES(?,1,?,?)", (i, f"public-{i}", f"{i:05d}"))
        conn.execute(
            "INSERT INTO embedding_fulltext_search(rowid,string_value) VALUES(?,?)", (i, doc)
        )
        metadata = [
            ("chroma:document", doc, None, None, None),
            ("wing", "target" if i >= 9000 else "other", None, None, None),
            ("count", None, i, None, None),
            ("ratio", None, None, i / 10000, None),
            ("flag", None, None, None, i % 2),
        ]
        metadata.extend((f"field-{k}", f"value-{i}-{k}", None, None, None) for k in range(12))
        conn.executemany(
            "INSERT INTO embedding_metadata VALUES(?,?,?,?,?,?)", [(i, *m) for m in metadata]
        )
    conn.commit()
    conn.close()
    collection = chroma.ChromaCollection(SimpleNamespace(name="drawers"), palace_path=directory)
    filters = [
        None,
        {"wing": "target"},
        {"$and": [{"count": {"$gte": 9500}}, {"flag": True}]},
        {"$or": [{"wing": "target"}, {"missing": {"$ne": "x"}}]},
        {"ratio": {"$lte": 0.2}},
        {"wing": {"$in": ["target"]}},
        {"missing": {"$nin": ["x"]}},
        {"wing": {"$contains": "arg"}},
    ]
    for query in ["needle", "xy", "absentword"]:
        for where in filters:
            args = dict(query=query, n_results=10, where=where)
            a, b = old(collection, **args), new(collection, **args)
            assert a == b, (query, where, a, b)
    for where in [{"wing": {"$in": ["target", "other"]}}, {"wing": "target"}]:
        timings = {"before": [], "after": []}
        for repeat in range(12):
            for label, method in (
                [("before", old), ("after", new)]
                if repeat % 2 == 0
                else [("after", new), ("before", old)]
            ):
                start = time.perf_counter()
                hits = method(collection, query="needle", n_results=10, where=where)
                timings[label].append((time.perf_counter() - start) * 1000)
        report["cases"].append(
            {
                "where": where,
                "timings_ms": timings,
                "median_ms": {k: statistics.median(v) for k, v in timings.items()},
                "ids": [h.id for h in hits],
            }
        )
    for column in ["bool_value", "float_value", "int_value"]:
        with sqlite3.connect(Path(directory) / "chroma.sqlite3") as conn:
            conn.execute(f"ALTER TABLE embedding_metadata DROP COLUMN {column}")
        for query in ["needle", "xy", "absentword"]:
            for where in filters:
                args = dict(query=query, n_results=10, where=where)
                assert old(collection, **args) == new(collection, **args), (column, query, where)
report["exact_output_pairs"] = 96
(OUT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
